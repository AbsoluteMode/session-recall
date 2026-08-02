"""One meta docs run: for every project, feed the distiller its pending
sessions one at a time — распаковка «обновлений по сессии».

Order is the safety property, twice over. Watermarks move only after a
chapter's documents are safely in the repo, so a crash or a garbled model
answer re-processes the same dialogue next run instead of silently swallowing
it. And sessions are strictly oldest-first: when one session fails, the
project HALTS for this run — later sessions build on earlier stories, and
distilling them out of order would write the ending before the beginning.
Other projects still run; each project that changed gets its own commit.
"""

import fcntl
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import collect
from .config import MetaConfig, Watermarks, state_path
from .distill import Distiller
from .repo import commit, current_docs, ensure_repo, write_docs


def acquire_lock(data_dir: Path) -> int | None:
    """One run at a time. Runs are hours-long during a backfill, so the
    nightly launchd job WILL overlap a manual run sooner or later — and two
    runs would race each other over watermarks and the git repo. flock, not a
    pid file: the lock dies with the process, so a crash never wedges the job.
    Returns the fd holding the lock, or None when another run owns it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / "metadocs.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


@dataclass
class RunReport:
    projects: list = field(default_factory=list)   # (project, done, pending, note)
    blocked: list = field(default_factory=list)    # (path, kinds) scanner stops
    commits: list = field(default_factory=list)    # (project, short sha)

    def summary(self) -> str:
        if not self.projects and not self.blocked:
            return "meta docs: nothing new"
        lines = []
        for project, done, pending, note in self.projects:
            line = f"{project}: {done}/{pending} session(s)"
            if note:
                line += f" — {note}"
            lines.append(line)
        for path, kinds in self.blocked:
            lines.append(f"BLOCKED by secret scanner: {path} [{', '.join(kinds)}] "
                         "— fix the source, the entry will retry next run")
        for project, sha in self.commits:
            lines.append(f"committed {sha} ({project})")
        return "\n".join(lines)


def run_once(cfg: MetaConfig, db, distiller: Distiller,
             data_dir: Path | None = None) -> RunReport:
    repo = Path(cfg.repo).expanduser()
    ensure_repo(repo)
    marks = Watermarks(state_path(data_dir))
    report = RunReport()

    for project in collect.select_projects(db, cfg.projects):
        sessions = collect.pending_sessions(db, project, marks)
        if not sessions:
            continue
        done, note, touched = 0, "", False
        for session in sessions:
            ok = True
            for chapter in collect.chapters(session.turns):
                updates = distiller(project, chapter, current_docs(repo, project))
                if updates is None:
                    note, ok = "distill failed, will retry", False
                    break
                wr = write_docs(repo, project, updates)
                report.blocked.extend(wr.blocked)
                if wr.blocked:
                    note, ok = "blocked by secret scanner", False
                    break
                touched = touched or bool(wr.written)
                collect.advance_marks(marks, chapter)
                marks.save()
            if not ok:
                break        # story order: later sessions wait for the retry
            done += 1
        if touched:
            sha = commit(repo, f"meta docs: {project} — {done} session(s)",
                         push=cfg.push)
            if sha:
                report.commits.append((project, sha))
        report.projects.append((project, done, len(sessions), note))
    return report
