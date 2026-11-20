"""Find every linked git worktree under the home directory.

Agents and tools (Claude Code's `.claude/worktrees`, fused-render branch
checkouts, `git worktree add` by hand) leave full repo checkouts behind, each
with its own node_modules / target / .venv. They are not caches, so the
Cleanup tab never saw them — on one machine 183 of them held 264 GB.

Discovery is two cheap steps, no full walk:

1. repos: a shallow scan of home (depth 4, heavy folders skipped) for
   directories holding a `.git` *directory* (a worktree has a `.git` file);
   memoised for an hour in .cache/repos.json.
2. worktrees: `git worktree list --porcelain` per repo, in parallel. That
   finds a repo's worktrees wherever they live, not only under the repo.

With `details` each worktree also gets: last commit time, whether its HEAD is
already contained in the repo's main branch ("merged"), uncommitted change
count, and its cached size (sizes themselves come from size_dir.py, page-side).
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import subprocess
import time

import catalog

REPOS_CACHE = os.path.join(catalog.CACHE_DIR, "repos.json")
SCAN_DEPTH = 4
# Never descend into these while looking for repos: huge, and never repos.
SKIP = {
    "Library", "node_modules", ".Trash", ".cache", ".npm", ".bun", ".cargo", ".rustup",
    ".venv", "venv", "venvs", ".claude", "target", "Applications", "Movies", "Music",
    "Pictures", ".git", "uv-cache", "site-packages", "__pycache__",
}


def git(args: list[str], cwd: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})


def _scan_repos() -> list[str]:
    repos: list[str] = []

    def walk(d: str, depth: int):
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            return
        for e in entries:
            if e.name == ".git" and e.is_dir(follow_symlinks=False):
                repos.append(d)
                return
        if depth == 0:
            return
        for e in entries:
            if e.name in SKIP:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    walk(e.path, depth - 1)
            except OSError:
                continue

    walk(catalog.HOME, SCAN_DEPTH)
    return sorted(repos)


def find_repos(refresh: bool = False, max_age: float = 3600.0) -> list[str]:
    if not refresh:
        try:
            with open(REPOS_CACHE) as fh:
                cached = json.load(fh)
            if time.time() - cached["scanned_at"] < max_age:
                return cached["repos"]
        except (OSError, ValueError, KeyError):
            pass
    repos = _scan_repos()
    os.makedirs(catalog.CACHE_DIR, exist_ok=True)
    tmp = "%s.%d.tmp" % (REPOS_CACHE, os.getpid())
    with open(tmp, "w") as fh:
        json.dump({"scanned_at": time.time(), "repos": repos}, fh)
    os.replace(tmp, REPOS_CACHE)
    return repos


def _parse_porcelain(text: str) -> list[dict]:
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:], "branch": None, "head": None, "locked": False, "prunable": False}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            cur["branch"] = None
        elif line.startswith("locked"):
            cur["locked"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
    return out


def repo_worktrees(repo: str) -> tuple[dict, list[dict]] | None:
    """(repo info, linked worktrees) — None when the repo has none."""
    try:
        r = git(["worktree", "list", "--porcelain"], repo)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    entries = _parse_porcelain(r.stdout)
    if len(entries) < 2:
        return None
    main = entries[0]
    info = {"path": repo, "name": os.path.basename(repo), "base": main["branch"], "count": len(entries) - 1}
    linked = []
    for e in entries[1:]:
        e["repo"] = repo
        e["name"] = os.path.basename(e["path"])
        linked.append(e)
    return info, linked


def list_all(refresh: bool = False) -> tuple[list[dict], list[dict]]:
    repos, worktrees = [], []
    with cf.ThreadPoolExecutor(16) as ex:
        for res in ex.map(repo_worktrees, find_repos(refresh)):
            if res:
                repos.append(res[0])
                worktrees.extend(res[1])
    return repos, worktrees


def _gitdir(worktree: str) -> str | None:
    try:
        with open(os.path.join(worktree, ".git")) as fh:
            line = fh.read().strip()
    except OSError:
        return None
    return line[8:] if line.startswith("gitdir: ") else None


def dirty_count(path: str, timeout: float = 8.0) -> int | None:
    """Tracked changes + untracked (not ignored) files; None if git timed out."""
    try:
        r = git(["status", "--porcelain", "--untracked-files=normal"], path, timeout)
    except subprocess.TimeoutExpired:
        return None
    return len(r.stdout.splitlines()) if r.returncode == 0 else None


def _details(wt: dict, bases: dict) -> dict:
    path = wt["path"]
    wt["exists"] = os.path.isdir(path)
    wt["committed"] = None
    wt["active"] = None
    wt["merged"] = None
    wt["dirty"] = None
    cached = catalog.read_size_cache(path, 7 * 86400)
    wt["size"] = cached["size"] if cached else None
    wt["truncated"] = bool(cached and cached.get("truncated"))
    wt["size_age"] = cached["age"] if cached else None
    if not wt["exists"]:
        return wt
    try:
        r = git(["log", "-1", "--format=%ct"], path)
        wt["committed"] = int(r.stdout.strip()) if r.stdout.strip() else None
        base = bases.get(wt["repo"])
        if base and wt["head"] and wt["branch"] != base:
            m = git(["merge-base", "--is-ancestor", wt["head"], base], wt["repo"])
            wt["merged"] = m.returncode == 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    gd = _gitdir(path)
    times: list[float] = [float(wt["committed"])] if wt["committed"] else []
    if gd:
        try:
            times.append(os.stat(os.path.join(gd, "index")).st_mtime)
        except OSError:
            pass
    wt["active"] = max(times) if times else None
    wt["dirty"] = dirty_count(path)
    return wt


def main(details: bool = False, refresh: bool = False):
    started = time.monotonic()
    repos, worktrees = list_all(refresh)
    if details:
        bases = {r["path"]: r["base"] for r in repos}
        with cf.ThreadPoolExecutor(16) as ex:
            worktrees = list(ex.map(lambda w: _details(w, bases), worktrees))
    else:
        for w in worktrees:
            cached = catalog.read_size_cache(w["path"], 7 * 86400)
            w["size"] = cached["size"] if cached else None
    repos.sort(key=lambda r: -r["count"])
    return {
        "repos": repos,
        "worktrees": worktrees,
        "size": sum(w.get("size") or 0 for w in worktrees),
        "now": time.time(),
        "elapsed": round(time.monotonic() - started, 2),
    }
