"""Git plumbing for the entries repository.

The agent writes entries through agent_server (which owns the secret scanner
and the dedup invariant); this module only guards the repository shape and
turns each run into commits — git is the review, undo and audit layer, which
is the whole reason the store is a repo and not a DB.
"""

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


def ensure_repo(repo: Path) -> None:
    """The target must be a git repository — meta docs without history is just
    a folder of files nobody can revert. Creates the directory and `git init`s
    it when missing; refuses to adopt a non-git directory silently."""
    repo.mkdir(parents=True, exist_ok=True)
    if _git(repo, "rev-parse", "--git-dir").returncode != 0:
        done = _git(repo, "init")
        if done.returncode != 0:
            raise RuntimeError(f"cannot init git repo at {repo}: {done.stderr.strip()}")


def has_changes(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain").stdout.strip())


def commit(repo: Path, message: str, push: bool = False) -> str:
    """One commit per unit of work, only when something changed. Returns the
    short hash, or "" when the tree was clean. Push failures are non-fatal:
    the commit is safe locally and the next successful push carries it."""
    _git(repo, "add", "-A")
    if not _git(repo, "status", "--porcelain").stdout.strip():
        return ""
    done = _git(repo, "commit", "-m", message)
    if done.returncode != 0:
        raise RuntimeError(f"git commit failed: {done.stderr.strip() or done.stdout.strip()}")
    short = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    if push:
        _git(repo, "push")
    return short
