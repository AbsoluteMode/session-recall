"""Pull the new dialogue out of the index — user words and final answers only.

Reads the existing index DB directly and read-only: the indexer already stores
exactly the surface layer meta docs wants (``user`` messages and the
assistant's answer text; tool calls, tool results and thinking never reach the
chunks table), so distilling needs no transcript parsing of its own and no
sqlite-vec extension — a plain sqlite3 connection suffices.
"""

import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ALL, PROJECT_GIT, Watermarks

# One run reads at most this much text per project. Whole sessions are taken
# oldest-first until the budget is hit; the rest simply waits for the next run
# (watermarks only advance over what was actually taken), so a giant day is
# processed across several runs instead of overflowing one model call.
BUDGET_CHARS = 60_000
MAX_TURN_CHARS = 4_000     # one pasted log must not eat the whole budget


@dataclass
class ProjectBatch:
    project: str
    turns: list = field(default_factory=list)    # {source, session_id, role, text, ts}
    spillover: bool = False    # true when the budget cut sessions off this run

    @property
    def chars(self) -> int:
        return sum(len(t["text"]) for t in self.turns)


def open_index(db_path: Path) -> sqlite3.Connection:
    """Read-only, and refuses to create: a missing index is a real error the
    caller must surface, not an empty DB silently distilled into nothing."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def is_git_checkout(cwd: str) -> bool:
    try:
        done = subprocess.run(["git", "-C", cwd, "rev-parse", "--git-dir"],
                              capture_output=True, timeout=10)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def select_projects(db: sqlite3.Connection, selector: list,
                    is_git=is_git_checkout) -> list[str]:
    """Resolve the config's project selector against what the index holds.

    "all" — every project ever indexed; "git" — projects whose working
    directory is (still) a git checkout; anything else — literal names.
    A project whose directory was deleted fails the git probe and drops out,
    which is the right default: no checkout, nowhere to apply the memory.
    """
    rows = db.execute(
        "SELECT project, MAX(cwd) FROM chunks WHERE project != '' "
        "GROUP BY project").fetchall()
    if PROJECT_ALL in selector:
        return sorted(p for p, _ in rows)
    if PROJECT_GIT in selector:
        return sorted(p for p, cwd in rows if cwd and is_git(cwd))
    wanted = set(selector)
    return sorted(p for p, _ in rows if p in wanted)


def new_dialogue(db: sqlite3.Connection, project: str,
                 marks: Watermarks) -> ProjectBatch:
    """Everything said in this project since the last run, oldest first,
    grouped so a session is either taken whole or left whole for next time."""
    rows = db.execute(
        "SELECT source, session_id, role, text, ts FROM chunks "
        "WHERE project = ? AND role IN ('user', 'assistant') "
        "ORDER BY ts, turn_index", (project,)).fetchall()

    sessions: dict[str, list] = {}
    for source, sid, role, text, ts in rows:
        if not text or not text.strip():
            continue
        ts = int(ts or 0)
        if ts <= marks.last_ts(source, sid):
            continue
        sessions.setdefault(f"{source}:{sid}", []).append(
            {"source": source, "session_id": sid, "role": role,
             "text": text[:MAX_TURN_CHARS], "ts": ts})

    batch = ProjectBatch(project=project)
    for _, turns in sorted(sessions.items(), key=lambda kv: kv[1][0]["ts"]):
        size = sum(len(t["text"]) for t in turns)
        if batch.turns and batch.chars + size > BUDGET_CHARS:
            batch.spillover = True
            break
        batch.turns.extend(turns)
    return batch


def advance_marks(marks: Watermarks, batch: ProjectBatch) -> None:
    """Only after the batch was distilled AND written — a crash in between
    must re-process, never silently skip."""
    for t in batch.turns:
        marks.advance(t["source"], t["session_id"], t["ts"])
