"""Remove selected git worktrees in a detached background job.

Same shape as clean_job.py (start / status / cancel / ack, a detached worker,
progress to a status file and to fused-render's activity panel), because
removing a few hundred GB of checkouts takes far longer than runPython's 60 s.

Safety rules:

* only paths that `git worktree list` currently reports as a *linked*
  worktree of a discovered repo are accepted — never a main checkout, never
  an arbitrary path the page sends;
* locked worktrees are refused;
* before a worktree with uncommitted work is removed, that work is saved as
  a patch (tracked changes plus untracked, non-ignored files, via an
  intent-to-add) under .fused/data/worktree-backups/<repo>/;
* branches are never deleted — `git worktree add <path> <branch>` brings a
  checkout back;
* after removal `git worktree prune` drops git's record of each one.
"""

import concurrent.futures as cf
import os
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
if __name__ == "__main__":
    os.chdir(HERE)
    sys.path.insert(0, HERE)

import catalog  # noqa: E402
import clean_job  # noqa: E402  (reuses its job-file helpers and activity-panel post)
import worktrees  # noqa: E402

JOB_FILE = os.path.join(catalog.CACHE_DIR, "worktree-job.json")
CANCEL_FILE = os.path.join(catalog.CACHE_DIR, "worktree-job.cancel")
BACKUP_DIR = os.path.join(HERE, ".fused", "data", "worktree-backups")
TERMINAL = ("done", "error", "cancelled")
MAX_UNTRACKED_IN_PATCH = 2000


def _load():
    import json
    try:
        with open(JOB_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write(job: dict):
    import json
    os.makedirs(catalog.CACHE_DIR, exist_ok=True)
    tmp = "%s.%d.tmp" % (JOB_FILE, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(job, fh)
    os.replace(tmp, JOB_FILE)


def _status():
    job = _load()
    if job is None:
        return {"state": "none"}
    if job["state"] == "running" and not clean_job._alive(job.get("pid")):
        job.update(state="error", detail="the removal worker exited unexpectedly")
        _write(job)
    return job


def validate(paths: list[str]) -> list[dict]:
    """Map requested paths to current linked worktrees, or raise."""
    _, linked = worktrees.list_all()
    by_path = {w["path"]: w for w in linked}
    chosen, bad = [], []
    for p in paths:
        w = by_path.get(os.path.normpath(p))
        if w is None:
            bad.append(p)
        elif w["locked"]:
            raise RuntimeError("worktree is locked (git worktree unlock it first): %s" % p)
        else:
            chosen.append(w)
    if bad:
        raise RuntimeError("not a linked git worktree: %s" % ", ".join(bad[:5]))
    return chosen


def main(action: str = "status", paths: str = "", confirm: bool = False,
         total: float = 0, origin: str = ""):
    if action == "status":
        return _status()
    if action == "cancel":
        job = _status()
        if job.get("state") == "running":
            open(CANCEL_FILE, "w").close()
        return job
    if action == "ack":
        job = _status()
        if job.get("state") in TERMINAL:
            job["acked"] = True
            _write(job)
        return job
    if action != "start":
        raise RuntimeError("unknown action %r" % action)

    if not confirm:
        raise RuntimeError("worktree_job.py refuses to start without confirm=true")
    if _status().get("state") == "running":
        raise RuntimeError("a worktree removal is already running")
    wanted = [p for p in paths.split("\n") if p.strip()]
    if not wanted:
        raise RuntimeError("no worktrees given")
    chosen = validate(wanted)

    try:
        os.remove(CANCEL_FILE)
    except OSError:
        pass
    job = {
        "id": "disk-cleaner-worktrees-" + uuid.uuid4().hex[:10],
        "title": "Disk Cleaner: removing worktrees",
        "items": [{"path": w["path"], "repo": w["repo"], "branch": w["branch"]} for w in chosen],
        "state": "running",
        "total": int(total or 0),
        "freed": 0,
        "removed": 0,
        "detail": "Starting…",
        "origin": os.environ.get("FUSED_RENDER_ORIGIN") or origin,
        "started": time.time(),
        "free_before": catalog_free(),
        "result": None,
        "acked": False,
    }
    _write(job)
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--worker"],
        cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    latest = _load() or job
    latest["pid"] = proc.pid
    _write(latest)
    return latest


def catalog_free() -> int:
    import shutil
    return shutil.disk_usage(catalog.HOME).free


# ---- worker ---------------------------------------------------------------

def backup(item: dict) -> str | None:
    """Save uncommitted work as a patch; returns the patch path or None if clean."""
    path = item["path"]
    n = worktrees.dirty_count(path, timeout=60)
    if n == 0:
        return None
    untracked = worktrees.git(["ls-files", "--others", "--exclude-standard", "-z"], path, 60).stdout
    files = [f for f in untracked.split("\0") if f]
    note = ""
    if files and len(files) <= MAX_UNTRACKED_IN_PATCH:
        # Intent-to-add makes `git diff` include untracked files' contents.
        # The worktree is about to be deleted, so touching its index is fine.
        worktrees.git(["add", "--intent-to-add", "--", *files], path, 60)
    elif files:
        note = "# %d untracked files were too many to include; their names:\n# %s\n" % (
            len(files), "\n# ".join(files[:500]))
    diff = worktrees.git(["diff", "HEAD", "--binary"], path, 120).stdout
    dest_dir = os.path.join(BACKUP_DIR, os.path.basename(item["repo"]))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "%s-%s.patch" % (os.path.basename(path), time.strftime("%Y%m%d-%H%M%S")))
    with open(dest, "w") as fh:
        fh.write("# worktree: %s\n# branch: %s\n# head: %s\n# restore: git worktree add <dir> %s && git -C <dir> apply %s\n%s\n" % (
            path, item.get("branch") or "(detached)",
            worktrees.git(["rev-parse", "HEAD"], path).stdout.strip(),
            item.get("branch") or "<commit>", dest, note))
        fh.write(diff)
    return dest


def _remove(item: dict, job: dict, lock: threading.Lock, errors: list, backups: list):
    path = item["path"]
    try:
        with lock:
            job["detail"] = os.path.basename(path)
        patch = backup(item)
        if patch:
            with lock:
                backups.append({"path": path, "patch": patch})
        cached = catalog.read_size_cache(path, 30 * 86400)
        r = subprocess.run(["rm", "-rf", path], capture_output=True, text=True)
        if os.path.exists(path):
            raise RuntimeError((r.stderr or "rm failed").strip().splitlines()[-1])
        with lock:
            job["removed"] += 1
            job["freed"] += cached["size"] if cached else 0
    except Exception as err:  # noqa: BLE001 — one failure must not stop the rest
        with lock:
            errors.append("%s: %s" % (path, err))


def _worker():
    job = _load()
    if not job or job.get("state") != "running":
        return
    job["pid"] = os.getpid()
    lock = threading.Lock()
    stop = threading.Event()
    finished = threading.Event()
    errors: list = []
    backups: list = []

    def report():
        with lock:
            snap = dict(job)
        _write(snap)
        if clean_job._post(snap):
            stop.set()

    def heartbeat():
        while not finished.wait(1.0):
            if os.path.exists(CANCEL_FILE):
                stop.set()
            report()

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    items = job["items"]
    try:
        # Items not yet started when Stop lands are skipped; ones mid-rm finish.
        with cf.ThreadPoolExecutor(4) as ex:
            futures = [ex.submit(lambda it=item: None if stop.is_set() else _remove(it, job, lock, errors, backups))
                       for item in items]
            for f in futures:
                f.result()
        for repo in sorted({i["repo"] for i in items}):
            worktrees.git(["worktree", "prune"], repo, 60)
        with lock:
            remaining = len(items) - job["removed"] - len(errors)
            job["freed"] = max(job["freed"], catalog_free() - job["free_before"])
            job["result"] = {
                "removed": job["removed"], "freed": job["freed"], "errors": errors,
                "backups": backups, "remaining": remaining,
            }
            if stop.is_set() and remaining:
                job["state"] = "cancelled"
                job["detail"] = "Stopped — %d worktree(s) left" % remaining
            else:
                job["state"] = "done"
                job["detail"] = "%d worktree(s) removed" % job["removed"]
    except Exception as err:  # noqa: BLE001 — must always reach a terminal state
        with lock:
            job["state"] = "error"
            job["detail"] = "%s: %s" % (type(err).__name__, err)
    finally:
        finished.set()
        beat.join(timeout=5)
        job["finished"] = time.time()
        try:
            os.remove(CANCEL_FILE)
        except OSError:
            pass
        report()


if __name__ == "__main__" and "--worker" in sys.argv:
    _worker()
