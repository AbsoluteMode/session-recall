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

@dataclass
class Turn:
    role: str
    type: str
    content: str
    raw: dict
