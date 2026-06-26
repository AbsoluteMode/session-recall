# src/session_recall/server.py
from dataclasses import asdict
from mcp.server.fastmcp import FastMCP
from .config import DB_PATH
from .store import Store
from .embed import make_embedder
from .rerank import make_reranker
from .retrieve import Recall

mcp = FastMCP("session-recall")
_recall: Recall | None = None


def build_recall() -> Recall:
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
def recall_search(query: str, k: int = 10, scope_cwd: str | None = None) -> list[dict]:
    """Semantically search past Claude Code sessions. Returns ranked anchors.

    scope_cwd: pass your current working directory to restrict results to the
    current project/repo (worktrees collapse to the repo root). Omit it for a
    global, cross-project search.
    """
    return [asdict(a) for a in _r().recall_search(query, k=k, scope_cwd=scope_cwd)]


@mcp.tool()
def expand_around(session_id: str, uuid: str, before: int = 2, after: int = 2) -> list[dict]:
    """Return the raw turns around an anchor (tool calls, outputs, thinking)."""
    return [asdict(t) for t in _r().expand_around(session_id, uuid, before, after)]


@mcp.tool()
def step(session_id: str, uuid: str, direction: str, count: int = 1) -> list[dict]:
    """Walk to an adjacent turn ('next' or 'prev')."""
    return [asdict(t) for t in _r().step(session_id, uuid, direction, count)]


@mcp.tool()
def grep(pattern: str, session_id: str | None = None, scope_cwd: str | None = None) -> list[dict]:
    """On-demand substring scan over raw session transcripts.

    scope_cwd: pass your current working directory to restrict the scan to the
    current project/repo; omit for a global scan.
    """
    return [asdict(a) for a in _r().grep(pattern, session_id, scope_cwd=scope_cwd)]


def main():
    mcp.run()


if __name__ == "__main__":
    main()
