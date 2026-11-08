"""What becomes a search chunk, and under which role.

Two kinds of turn used to enter the index as ordinary conversation and then
outranked the conversation itself:

* a compacted session's summary — a 20–70 KB digest the harness writes into
  a `user` turn. Dense in every keyword of the session, so it matches almost
  any query about that project, yet it is derivative: every fact in it also
  lives in the turns it summarizes. Role `summary`: embedded, but left out of
  recall_search unless asked for.
* an automation heartbeat — the same `<heartbeat>` prompt and `<heartbeat>`
  reply every few minutes, differing only in a timestamp, so content-hash
  dedup never collapses them. One Codex automation produced 9 169 of the
  40 035 chunks in a real index. Role `automation`: kept as a row (so the
  session still shows on the status board), never embedded, never ranked.

Long turns are also split into overlapping sub-chunks: a single vector for
a 25 KB answer is an average of everything it says, and the embedder
truncates at a fixed token budget, so the tail of a long turn was invisible
to semantic search. Sub-chunks share the turn's uuid; recall collapses them
back to one hit.

# WHY: docs/decisions/2026-11-08-recall-noise-recency-subchunks.md
"""

import re

from .snippets import is_compaction

_HEARTBEAT = "<heartbeat>"
# Roles that never get a vector: nothing in them is worth finding.
_UNEMBEDDED = frozenset({"automation"})
# Roles ranked by recall_search by default.
DEFAULT_ROLES = ("user", "assistant", "doc")

MAX_CHARS = 2000
OVERLAP = 200
# split_text's overlap is OVERLAP minus at most one word, so a shared run
# shorter than this is coincidence ("redis" + "disk cache"), not a seam.
MIN_JOIN_OVERLAP = 32
_BREAKS = ("\n\n", "\n", ". ", " ")
_WS = re.compile(r"\s")


def surface_role(role: str, text: str) -> str:
    """The role a surface turn is indexed under."""
    if text.lstrip().startswith(_HEARTBEAT):
        return "automation"
    if role == "user" and is_compaction(text):
        return "summary"
    return role


def embeddable(role: str) -> bool:
    return role not in _UNEMBEDDED


def split_text(text: str, max_chars: int = MAX_CHARS,
               overlap: int = OVERLAP) -> list[str]:
    """Cut `text` into windows of at most `max_chars`, overlapping by about
    `overlap` characters, breaking at a paragraph, line, sentence or word
    boundary in the second half of the window when one exists. Short text
    comes back as a single piece, unchanged."""
    if len(text) <= max_chars:
        return [text]
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    overlap = max(0, min(overlap, max_chars // 2))
    pieces: list[str] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + max_chars, n)
        if end < n:
            floor = start + max_chars // 2
            for sep in _BREAKS:
                pos = text.rfind(sep, floor, end)
                if pos > start:
                    end = pos + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= n:
            break
        nxt = end - overlap
        ws = _WS.search(text, nxt, end)
        if ws is not None and ws.end() < end:
            nxt = ws.end()
        start = nxt if nxt > start else end
    return pieces


def join_pieces(first: str, second: str, sep: str = "\n") -> str:
    """Concatenate two consecutive pieces of one turn without repeating the
    overlap they share. Only a seam-sized overlap counts: two separate text
    blocks of one turn that happen to share a few characters are joined
    whole, with `sep` between them."""
    limit = min(len(first), len(second), OVERLAP * 2)
    for n in range(limit, MIN_JOIN_OVERLAP - 1, -1):
        if first.endswith(second[:n]):
            return first + second[n:]
    return first + sep + second


def surface_pieces(role: str, text: str) -> list[str]:
    """The index rows one surface turn becomes. Summaries and automation
    stay whole: the former is excluded by default and reuses its vector
    across re-index runs, the latter is never embedded."""
    if role in ("user", "assistant"):
        return split_text(text)
    return [text]
