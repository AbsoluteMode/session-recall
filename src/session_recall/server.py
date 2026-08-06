# src/session_recall/server.py
import time
from dataclasses import asdict
from mcp.server.fastmcp import FastMCP
from .config import DB_PATH
from .store import Store
from .embed import make_embedder
from .rerank import make_reranker
from .retrieve import Recall
from .timefmt import date_range_to_epoch, humanize_ts

mcp = FastMCP("session-recall")
_recall: Recall | None = None


def _adict(a) -> dict:
    # Enrich an Anchor with a human-readable timestamp; `when` stays the raw epoch
    # for sorting, `when_human` makes "now vs old" legible at the tool boundary.
    d = asdict(a)
    d["when_human"] = humanize_ts(a.when, int(time.time()))
    # A solo index has exactly one owner, so the field would be a null on every
    # anchor forever. Dropped rather than sent: an agent reading "owner": null
    # ten times learns nothing, and the hub keeps the field meaningful.
    if d.get("owner") is None:
        d.pop("owner", None)
    return d


def build_recall():
    """The local index, or the team hub when this machine has joined one.

    The five tools below do not change shape either way — that is the point.
    A member's agent asks the same questions and gets the same answers; only
    the corpus is bigger and the embedding happens on the server.

    Deciding here (rather than per tool) keeps the solo install untouched: no
    hub config, no import, no behaviour change.
    """
    from .hub.client import HubConfig
    cfg = HubConfig.load()
    if cfg is not None:
        from .hub.remote import RemoteRecall
        return RemoteRecall(cfg)
    return Recall(Store(DB_PATH), make_embedder(), make_reranker())


def _r() -> Recall:
    # Lazy single-instance init. Assumes single-threaded asyncio use (the FastMCP
    # event loop); the None-check race is benign under CPython's GIL for a local
    # single-user server.
    global _recall
    if _recall is None:
        _recall = build_recall()
    return _recall


@mcp.tool()
def recall_search(query: str, k: int = 10, scope_cwd: str | None = None,
                  source: str | None = None, start_date: str | None = None,
                  end_date: str | None = None, timezone: str | None = None,
                  on_date: str | None = None) -> dict:
    """Semantically search past Claude Code, Codex and Cursor sessions.

    Returns {"anchors": [...ranked anchors...], "degraded": null | str}.

    degraded is non-null when the embedding provider was unreachable and the search
    fell back to literal keyword matching: semantic ranking is OFF, so a thin result
    set means "not phrased this way in the transcript", NOT "not in the history".
    Retry with the exact identifiers you expect (error strings, symbols, file names),
    tell the user the search is degraded, and treat a miss as inconclusive.

    scope_cwd: pass your current working directory to restrict results to the
    current project/repo (worktrees collapse to the repo root). Omit it for a
    global, cross-project search.
    source: optionally restrict to "claude", "codex", or "cursor"; omit for the
    shared index.
    start_date/end_date: inclusive local calendar dates (YYYY-MM-DD). Either may
    be omitted for an open-ended range.
    on_date: shorthand for one local calendar day; cannot be combined with a range.
    timezone: IANA timezone override; omit it to use the user's computer timezone.
    """
    start_ts, end_ts = date_range_to_epoch(
        start_date, end_date, timezone, on_date=on_date)
    hits = _r().recall_search(
        query, k=k, scope_cwd=scope_cwd, source=source,
        start_ts=start_ts, end_ts=end_ts)
    return {"anchors": [_adict(a) for a in hits],
            "degraded": getattr(hits, "degraded", None)}


@mcp.tool()
def expand_around(session_id: str, uuid: str, before: int = 2, after: int = 2,
                  source: str | None = None) -> list[dict]:
    """Return the raw turns around an anchor (tool calls, outputs, thinking)."""
    return [asdict(t) for t in _r().expand_around(
        session_id, uuid, before, after, source=source)]


@mcp.tool()
def step(session_id: str, uuid: str, direction: str, count: int = 1,
         source: str | None = None) -> list[dict]:
    """Walk to an adjacent turn ('next' or 'prev')."""
    return [asdict(t) for t in _r().step(
        session_id, uuid, direction, count, source=source)]


@mcp.tool()
def grep(pattern: str, session_id: str | None = None, scope_cwd: str | None = None,
         source: str | None = None, limit: int = 100,
         start_date: str | None = None, end_date: str | None = None,
         timezone: str | None = None, on_date: str | None = None) -> list[dict]:
    """On-demand substring scan over raw session transcripts.

    scope_cwd: pass your current working directory to restrict the scan to the
    current project/repo; omit for a global scan.
    source: optionally restrict to "claude", "codex", or "cursor".
    limit: maximum number of matches returned (default 100).
    start_date/end_date: inclusive local calendar dates (YYYY-MM-DD).
    on_date: shorthand for one local calendar day; cannot be combined with a range.
    timezone: IANA timezone override; omit it to use the user's computer timezone.
    """
    start_ts, end_ts = date_range_to_epoch(
        start_date, end_date, timezone, on_date=on_date)
    return [_adict(a) for a in _r().grep(
        pattern, session_id, scope_cwd=scope_cwd, source=source, limit=limit,
        start_ts=start_ts, end_ts=end_ts)]


@mcp.tool()
def recent_sessions(scope_cwd: str | None = None, limit: int = 10,
                    source: str | None = None, start_date: str | None = None,
                    end_date: str | None = None,
                    timezone: str | None = None,
                    on_date: str | None = None) -> list[dict]:
    """List the most recently active past sessions, freshest first — use to see the
    current state of work and how fresh the index is (the top entry's
    last_activity_human is the effective freshness). Also surfaces the sessions of a
    thread split across resume-created session_ids so you can reassemble the arc.

    scope_cwd: pass your current working directory to restrict to the current
    project/repo (worktrees collapse to the repo root); omit for all projects.
    source: optionally restrict to "claude", "codex", or "cursor".
    start_date/end_date: inclusive local calendar dates (YYYY-MM-DD).
    on_date: shorthand for one local calendar day; cannot be combined with a range.
    timezone: IANA timezone override; omit it to use the user's computer timezone.
    Each entry: source, session_id, project, turns, last_activity (epoch),
    last_activity_human, label (the session's first user prompt).
    """
    start_ts, end_ts = date_range_to_epoch(
        start_date, end_date, timezone, on_date=on_date)
    return _r().recent_sessions(
        scope_cwd=scope_cwd, limit=limit, source=source,
        start_ts=start_ts, end_ts=end_ts)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
