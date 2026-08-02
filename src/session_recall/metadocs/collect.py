"""Pull the new dialogue out of the index — user words and final answers only,
handed over SESSION BY SESSION.

The session is the unit of work on purpose: one session is one work arc (a bug
hunt, a feature, a decision), and the per-session watermark makes it the
natural increment — a run gives the distiller each session's unseen tail,
oldest session first, until nothing is pending. There is no run-level budget:
runs take as long as the backlog demands. The only size threshold lives at the
level of ONE model call — a marathon session is split into sequential chapters,
because a single call has practical limits even though the run does not.

Reads the existing index DB directly and read-only: the indexer already stores
exactly the surface layer meta docs wants (``user`` messages and the
assistant's answer text; tool calls, tool results and thinking never reach the
chunks table), so distilling needs no transcript parsing of its own and no
sqlite-vec extension — a plain sqlite3 connection suffices.
"""

import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ALL, PROJECT_GIT, Watermarks

MAX_CALL_CHARS = 60_000    # per-CALL ceiling: chapter split for marathon sessions
MAX_TURN_CHARS = 4_000     # one pasted log must not eat a whole chapter

# A "project" whose name is a bare UUID or an mkdtemp basename is index junk,
# never a real codebase. The tmp pattern is load-bearing: print-mode agent
# calls (the distiller itself, the share composer) run in temp dirs and leave
# transcripts there — without this filter the distiller EATS ITS OWN EXHAUST,
# distilling transcripts of previous distill calls in a self-feeding loop
# (observed live: 360 tmp "projects" in one evening).
_JUNK_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|tmp[-_a-z0-9]{8})$", re.I)   # mkdtemp: tmp + exactly 8 random chars


@dataclass
class SessionUpdate:
    """One session's dialogue the distiller has not seen yet."""
    source: str
    session_id: str
    turns: list = field(default_factory=list)   # {source, session_id, role, text, ts}


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
        return sorted(p for p, _ in rows if not _JUNK_RE.match(p))
    if PROJECT_GIT in selector:
        return sorted(p for p, cwd in rows
                      if not _JUNK_RE.match(p) and cwd and is_git(cwd))
    # explicit names are deliberate — no junk filter applied
    wanted = set(selector)
    return sorted(p for p, _ in rows if p in wanted)


def pending_sessions(db: sqlite3.Connection, project: str, marks: Watermarks,
                     since: float = 0.0,
                     until: float | None = None) -> list[SessionUpdate]:
    """Every session with dialogue newer than its watermark, oldest first.
    A late-indexed old session has no mark and is picked up whole; an appended
    session contributes only its tail — «обновления по сессии». `since` is the
    start-of-memory (dialogue older than it is nobody's backlog); `until` caps
    the window from above — `index-history` uses it to stop exactly where the
    daily memory begins, so the two never overlap."""
    rows = db.execute(
        "SELECT source, session_id, role, text, ts FROM chunks "
        "WHERE project = ? AND role IN ('user', 'assistant') "
        "ORDER BY ts, turn_index", (project,)).fetchall()

    per: dict[str, SessionUpdate] = {}
    for source, sid, role, text, ts in rows:
        if not text or not text.strip():
            continue
        ts = int(ts or 0)
        if ts < since or (until is not None and ts >= until):
            continue
        if ts <= marks.last_ts(source, sid):
            continue
        upd = per.setdefault(f"{source}:{sid}",
                             SessionUpdate(source=source, session_id=sid))
        upd.turns.append({"source": source, "session_id": sid, "role": role,
                          "text": text[:MAX_TURN_CHARS], "ts": ts})
    return sorted(per.values(), key=lambda u: u.turns[0]["ts"])


def chapters(turns: list, ceiling: int | None = None) -> list[list]:
    """A session almost always fits one call. A marathon session is split into
    sequential chapters, in order; each later chapter's call sees the documents
    the earlier chapters already updated, so the story stays continuous."""
    ceiling = MAX_CALL_CHARS if ceiling is None else ceiling
    out, cur, size = [], [], 0
    for t in turns:
        if cur and size + len(t["text"]) > ceiling:
            out.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t["text"])
    if cur:
        out.append(cur)
    return out


def advance_marks(marks: Watermarks, turns: list) -> None:
    """Only after these turns were distilled AND written — a crash in between
    must re-process, never silently skip."""
    for t in turns:
        marks.advance(t["source"], t["session_id"], t["ts"])
