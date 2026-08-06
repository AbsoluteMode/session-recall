from dataclasses import dataclass

@dataclass
class Chunk:
    session_id: str
    uuid: str
    role: str
    text: str
    project: str
    cwd: str
    git_branch: str
    ts: int
    file_path: str
    byte_offset: int
    byte_len: int
    turn_index: int
    content_hash: str
    # Transcript producer. Kept last with a default so callers constructing
    # synthetic/legacy chunks remain source-compatible.
    source: str = "claude"

@dataclass
class Anchor:
    session_id: str
    uuid: str
    role: str
    snippet: str
    # None = keyword-only recall_search hit (relevance unknown: no vector distance,
    # no rerank). grep anchors keep 1.0 — an exact substring match by construction.
    score: "float | None"
    project: str
    when: int
    source: str = "claude"
    # Whose history this came from. None on a solo install, where there is only
    # one person's sessions and the question never arises; filled on a team hub,
    # where "who did this" decides whether you trust the hit and who to ask.
    # A field rather than an extra dict key: the hub client and the MCP layer
    # both serialise strictly by these fields, and would drop anything else.
    # WHY: docs/decisions/2026-08-06-team-hub-central-index.md
    owner: "str | None" = None

@dataclass
class Turn:
    role: str
    type: str
    content: str
    raw: dict
    source: str = "claude"
