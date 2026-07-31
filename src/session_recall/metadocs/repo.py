"""Writing distilled documents into the target git repository.

Layout: ``<repo>/<project>/{bugs,actions,decisions}.md`` and ``<repo>/USER.md``.
Every run that changed anything ends in one commit — git is the review, undo
and audit layer, which is the whole reason the store is a repo and not a DB.

The secret scanner runs HERE, on the final text, because this is the last
gate before bytes reach a repository that may later be pushed or shared: a
flagged document is not written at all and the run reports why. Fail closed,
same rule as share (a flagged answer never leaves through a side channel).
"""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..share.scanner import scan
from .distill import PROJECT_FILES, USER_FILE


@dataclass
class WriteReport:
    written: list = field(default_factory=list)     # repo-relative paths
    blocked: list = field(default_factory=list)     # (path, kinds) the scanner stopped
    committed: str = ""                             # commit hash, "" if nothing changed


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


def current_docs(repo: Path, project: str) -> dict:
    """What the distiller sees as the current state, including USER.md —
    the user map is global, but every project run may extend it."""
    docs = {}
    for name in PROJECT_FILES:
        p = repo / project / name
        docs[name] = p.read_text() if p.exists() else ""
    up = repo / USER_FILE
    docs[USER_FILE] = up.read_text() if up.exists() else ""
    return docs


def write_docs(repo: Path, project: str, updates: dict) -> WriteReport:
    """Write whatever survived the scanner. Unknown filenames were already
    dropped by the output parser; this trusts its input that far and no
    further — the content itself still gets scanned."""
    report = WriteReport()
    for name, content in updates.items():
        findings = scan(content)
        rel = name if name == USER_FILE else f"{project}/{name}"
        if findings:
            kinds = sorted({f.kind for f in findings})
            report.blocked.append((rel, kinds))
            continue
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text() == content:
            continue
        path.write_text(content)
        report.written.append(rel)
    return report


def commit(repo: Path, message: str, push: bool = False) -> str:
    """One commit per run, only when something changed. Returns the short
    hash, or "" when the tree was clean. Push failures are non-fatal: the
    commit is safe locally and the next successful push carries it."""
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
