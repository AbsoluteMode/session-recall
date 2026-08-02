"""One meta docs run: for every project, hand each pending session to the
distiller agent, which writes entries through its four tools.

Order is the safety property, twice over. Watermarks move only after the
agent's call finished cleanly (its writes are already on disk by then), so a
crash or a failed call re-processes the same dialogue next run instead of
silently swallowing it. And sessions are strictly oldest-first: when one
session fails, the project HALTS for this run — later sessions build on
earlier stories, and distilling them out of order would write the ending
before the beginning. Other projects still run; each project that changed
gets its own commit, so the run's whole effect is reviewable diff by diff.

If the repo still holds the old category-file format, it is migrated to
entry-per-file first, as its own commit.
"""

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import collect, entries
from .config import MetaConfig, Watermarks, state_path
from .distill import Distiller
from .repo import commit, ensure_repo, has_changes

# re-exported for cli.py; lives here so locking stays next to what it guards
from .lock import acquire_lock  # noqa: F401


@dataclass
class RunReport:
    projects: list = field(default_factory=list)   # (project, done, pending, note)
    commits: list = field(default_factory=list)    # (project, short sha)
    migrated: int = 0

    def summary(self) -> str:
        if not self.projects and not self.migrated:
            return "meta docs: nothing new"
        lines = []
        if self.migrated:
            lines.append(f"migrated {self.migrated} entr(y/ies) to per-file format")
        for project, done, pending, note in self.projects:
            line = f"{project}: {done}/{pending} session(s)"
            if note:
                line += f" — {note}"
            lines.append(line)
        for project, sha in self.commits:
            lines.append(f"committed {sha} ({project})")
        return "\n".join(lines)


def run_once(cfg: MetaConfig, db, distiller: Distiller,
             data_dir: Path | None = None) -> RunReport:
    repo = Path(cfg.repo).expanduser()
    ensure_repo(repo)
    marks = Watermarks(state_path(data_dir))
    report = RunReport()

    if entries.needs_migration(repo):
        report.migrated = entries.migrate(repo)
        commit(repo, "meta docs: migrate to entry-per-file format")

    for project in collect.select_projects(db, cfg.projects):
        sessions = collect.pending_sessions(db, project, marks, since=cfg.since)
        if not sessions:
            continue
        done, note = 0, ""
        for session in sessions:
            key = f"{session.source}:{session.session_id}"
            ok = True
            for chapter in collect.chapters(session.turns):
                if distiller(project, key, chapter) is None:
                    note, ok = "distill failed, will retry", False
                    break
                collect.advance_marks(marks, chapter)
                marks.save()
            if not ok:
                break        # story order: later sessions wait for the retry
            done += 1
        if has_changes(repo):
            sha = commit(repo, f"meta docs: {project} — {done} session(s)",
                         push=cfg.push)
            if sha:
                report.commits.append((project, sha))
        report.projects.append((project, done, len(sessions), note))
        # stream progress as it happens: a backlog run lasts hours, and a log
        # that stays silent until the very end is indistinguishable from a hang
        print(f"[metadocs {time.strftime('%H:%M')}] {project}: "
              f"{done}/{len(sessions)} session(s)"
              f"{' — ' + note if note else ''}", file=sys.stderr, flush=True)
    return report
