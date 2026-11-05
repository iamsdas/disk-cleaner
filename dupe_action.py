"""Act on one set of identical files from the last duplicate scan.

dupes.py only reports. This is the step that changes files, so it is narrow:

* it takes a set's **hash** plus a path from that set, never free paths — only
  files listed in `.cache/dupes-result.json` can be touched;
* every copy is re-checked right before it is touched: still a regular file,
  same size and inode as when compared, not hard-linked, owned by you, and
  byte-for-byte equal to the copy being kept (a hash match alone is not enough
  to change a file);
* `merge` (APFS only) replaces each extra copy with a clone of the kept one.
  Every path still exists with the same contents, permissions, dates and
  extended attributes; the copies just share their blocks until one is edited,
  so the space comes back without deleting anything;
* `trash` moves every copy except the kept one to the Trash (reversible). On
  macOS it goes through Finder so "Put Back" works; the result lists any copy
  that had to fall back to a plain move (no Put Back) by its original path.

After acting it rewrites the saved result (set savings, totals, per-folder
credit) so the page shows what is left without a rescan.

Actions: reveal | merge | trash. Each call does one set and stays well under
runPython's 60 s limit; the page loops over sets for "merge all".
"""

import ctypes
import ctypes.util
import os
import stat as statmod
import subprocess
import sys
import time

import dupes

DEADLINE = 45.0          # seconds of work per call before stopping early
CLONE_NOFOLLOW = 0x0001
COPYFILE_METADATA = 0x7  # ACL | STAT | XATTR


def _libc():
    return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _find_set(result: dict, digest: str) -> dict:
    for s in result.get("sets", []):
        if s["hash"] == digest:
            return s
    raise RuntimeError("that set is no longer in the scan result — scan again")


def _same_bytes(a: str, b: str, deadline: float) -> bool:
    with open(a, "rb", buffering=0) as fa, open(b, "rb", buffering=0) as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if x != y:
                return False
            if not x:
                return True
            if time.time() > deadline:
                raise TimeoutError


def _check(path: str, size: int):
    """lstat of a copy that is still safe to touch, else a reason string."""
    try:
        st = os.lstat(path)
    except OSError as err:
        return None, "gone (%s)" % err.strerror
    flags = getattr(st, "st_flags", 0)
    if not statmod.S_ISREG(st.st_mode):
        return None, "no longer a regular file"
    if st.st_size != size:
        return None, "size changed since the scan"
    if st.st_nlink > 1:
        return None, "hard-linked"
    if st.st_uid != os.getuid():
        return None, "owned by another user"
    if flags & (dupes.SF_DATALESS | dupes.UF_IMMUTABLE | dupes.SF_IMMUTABLE):
        return None, "locked or a cloud placeholder"
    return st, None


def _clone_over(keep: str, target: str, st_before):
    """Replace `target` with an APFS clone of `keep`, keeping target's metadata."""
    libc = _libc()
    tmp = os.path.join(os.path.dirname(target), ".%s.dc-clone-%d" % (os.path.basename(target), os.getpid()))
    if libc.clonefile(keep.encode(), tmp.encode(), CLONE_NOFOLLOW) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    try:
        # Permissions, flags, dates, ACLs and xattrs come from the copy being
        # replaced, so from the outside nothing about that path changes.
        if libc.copyfile(target.encode(), tmp.encode(), None, COPYFILE_METADATA) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        os.utime(tmp, ns=(st_before.st_atime_ns, st_before.st_mtime_ns), follow_symlinks=False)
        now = os.lstat(target)
        if (now.st_ino, now.st_size, now.st_mtime_ns) != (st_before.st_ino, st_before.st_size, st_before.st_mtime_ns):
            raise RuntimeError("changed while merging")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _trash(path: str) -> bool:
    """Move one copy to the Trash. True when Finder's "Put Back" will work.

    On macOS this goes through Finder, which records where the file came from
    (a plain rename into ~/.Trash does not, so Put Back would be greyed out).
    The first time, macOS asks whether this app may control Finder; if that is
    refused or Finder isn't reachable, fall back to the plain move.
    """
    if sys.platform == "darwin":
        script = 'on run argv\ntell application "Finder" to delete (POSIX file (item 1 of argv) as alias)\nend run'
        try:
            r = subprocess.run(["/usr/bin/osascript", "-e", script, path],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and not os.path.lexists(path):
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if not os.path.lexists(path):
            return True
    import clean                           # deferred: only the fallback needs it
    clean._trash(path)
    return sys.platform != "darwin"        # XDG trash keeps a .trashinfo, so restore works


# ---- keeping the saved result in step -----------------------------------------

def _credit(result: dict, s: dict, sign: int):
    """Add or remove this set's share of the per-folder savings (as dupes.py)."""
    paths = s["paths"]
    if len(paths) < 2 or s["savings"] <= 0:
        return
    root = result["root"]
    share = s["savings"] / (len(paths) - 1)
    folders = {f["name"]: f for f in result.get("folders", [])}
    for p in paths[1:]:
        rel = os.path.relpath(p, root)
        top = rel.split(os.sep, 1)[0] if os.sep in rel else "(files in %s)" % os.path.basename(root)
        f = folders.setdefault(top, {"name": top, "savings": 0, "copies": 0, "path": os.path.join(root, top)})
        f["savings"] = max(0, round(f["savings"] + sign * share))
        f["copies"] = max(0, f["copies"] + sign)
    result["folders"] = sorted((f for f in folders.values() if f["savings"] > 0), key=lambda f: -f["savings"])


def _restat(s: dict):
    clones = dupes._CloneInfo()
    ids = [clones.get(p) for p in s["paths"]]
    stored = len({cid for cid, _ in ids}) if ids and all(ids) else len(s["paths"])
    s["stored"] = stored
    s["savings"] = max(0, (stored - 1) * s["size"])


def _apply(result: dict, s: dict, new_paths: list):
    before_savings, before_shared = s["savings"], (len(s["paths"]) - s.get("stored", len(s["paths"]))) * s["size"]
    _credit(result, s, -1)
    s["paths"] = sorted(new_paths)
    _restat(s)
    after_shared = (len(s["paths"]) - s["stored"]) * s["size"]
    _credit(result, s, +1)
    result["savings"] = max(0, result["savings"] - before_savings + s["savings"])
    result["already_shared"] = max(0, result.get("already_shared", 0) - before_shared + after_shared)
    if s["savings"] <= 0:
        result["sets"] = [x for x in result["sets"] if x is not s]
        result["sets_total"] = max(0, result["sets_total"] - 1)
    dupes._save(dupes.RESULT_FILE, result)
    return before_savings - s["savings"]


# ---- actions -------------------------------------------------------------------

def _reveal(result: dict, digest: str, path: str):
    s = _find_set(result, digest)
    if path not in s["paths"]:
        raise RuntimeError("that file is not part of this set")
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", "-R", path], check=False, timeout=10)
    else:
        subprocess.run(["xdg-open", os.path.dirname(path)], check=False, timeout=10)
    return {"ok": True}


def main(action: str = "", hash: str = "", keep: str = "", path: str = "", confirm: str = ""):
    """reveal | merge | trash for one set from the last scan."""
    result = dupes._load(dupes.RESULT_FILE)
    if not result or result.get("none"):
        raise RuntimeError("no duplicate scan to act on — run a scan first")
    if action == "reveal":
        return _reveal(result, hash, path)
    if action not in ("merge", "trash"):
        raise RuntimeError("unknown action %r" % action)
    if confirm != "true":
        raise RuntimeError("refusing to change files without confirm=true")
    if dupes._status().get("state") == "running":
        raise RuntimeError("a duplicate scan is running — wait for it to finish")
    if action == "merge" and not (sys.platform == "darwin" and result.get("fs") == "apfs"):
        raise RuntimeError("merging copies needs an APFS disk; use “Trash other copies” instead")

    s = _find_set(result, hash)
    keep = keep or s["paths"][0]
    if keep not in s["paths"]:
        raise RuntimeError("the copy to keep is not part of this set")
    keep_st, why = _check(keep, s["size"])
    if keep_st is None:
        raise RuntimeError("the copy to keep is %s — scan again" % why)

    deadline = time.time() + DEADLINE
    clones = dupes._CloneInfo()
    keep_id = clones.get(keep)
    done, skipped, remaining, no_put_back = [], [], [], []
    kept_paths = [keep]
    others = [p for p in s["paths"] if p != keep]
    for i, p in enumerate(others):
        if time.time() > deadline:
            remaining = others[i:]
            kept_paths += remaining
            break
        st, why = _check(p, s["size"])
        if st is None:
            skipped.append({"path": p, "why": why})
            if os.path.lexists(p):
                kept_paths.append(p)
            continue
        if action == "merge":
            cid = clones.get(p)
            if keep_id and cid and cid[0] == keep_id[0]:
                kept_paths.append(p)       # already shares the kept copy's blocks
                continue
        try:
            if not _same_bytes(keep, p, deadline):
                skipped.append({"path": p, "why": "contents differ from the kept copy"})
                kept_paths.append(p)
                continue
            if action == "merge":
                _clone_over(keep, p, st)
                kept_paths.append(p)
            else:
                if not _trash(p):
                    no_put_back.append(p)
            done.append(p)
        except TimeoutError:
            remaining = others[i:]
            kept_paths += remaining
            break
        except (OSError, RuntimeError) as err:
            skipped.append({"path": p, "why": str(err)})
            kept_paths.append(p)

    freed = _apply(result, s, kept_paths)
    return {
        "action": action, "hash": hash, "done": len(done), "done_paths": done,
        "no_put_back": no_put_back, "skipped": skipped,
        "remaining": len(remaining), "freed": freed,
        "set_left": s["savings"] > 0, "savings": result["savings"],
    }
