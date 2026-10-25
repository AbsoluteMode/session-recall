"""Short human-readable text for tool output: hit snippets and session labels.

A snippet is the only part of a hit an agent reads before deciding to expand
it. The first 200 characters of a chunk are often the wrong 200: a compacted
session's summary opens with the same boilerplate sentence every time, and a
long answer mentions the asked-about thing only halfway down. So a snippet is
cut around the passage that matches the query, and boilerplate that every
such chunk shares is dropped first.

The same problem hits session labels: the first "user" message of a session
is often not a person asking anything — a side-session brief, a desktop
event, a pasted block, a Codex heartbeat. `human_ask` finds the actual ask.

# WHY: docs/decisions/2026-10-25-recall-for-registrar.md
"""

import re

_COMPACTION = re.compile(
    r"^This session is being continued from a previous conversation[^\n]*\n"
    r"(?:[^\n]*\n){0,3}?\s*Summary:\s*", re.IGNORECASE)
_WORD = re.compile(r"\w+", re.UNICODE)

# Machine blocks that reach the index inside user turns. Matched pairs only,
# like extract._HARNESS_BLOCK; content of a pasted block is the user's own.
_EVENT_BLOCK = re.compile(
    r"<(side-sessions-event|ci-monitor-event|task-notification|system-reminder)"
    r"\b[^>]*>.*?</\1>", re.DOTALL)
_PASTED_TAG = re.compile(r"</?pasted_content\b[^>]*>")
_BRIEF = "Background (from the session that started this one)"
_BRIEF_TASK = re.compile(r"(?m)^Task:\s*")


def strip_boilerplate(text: str) -> str:
    """Drop the fixed preamble of a compacted-session summary."""
    return _COMPACTION.sub("", text, count=1)


def _stem(term: str) -> str:
    # Crude, language-agnostic prefix match: "Гармония"/"Гармонии",
    # "indexing"/"indexed" meet on a shared prefix.
    return term if len(term) <= 4 else term[:max(4, len(term) - 2)]


def query_snippet(text: str, query: str, n: int = 200) -> str:
    """A ≤n-char window of `text` that covers the most distinct query terms.

    Falls back to the opening of the text when no term occurs literally —
    a purely semantic hit has no better anchor than its start."""
    body = strip_boilerplate(text).strip() or text
    stems = {_stem(w.lower()) for w in _WORD.findall(query or "") if len(w) >= 3}
    lower = body.lower()
    best_start, best_score = 0, 0
    if stems and len(body) > n:
        for stem in stems:
            start = 0
            for _ in range(50):
                pos = lower.find(stem, start)
                if pos < 0:
                    break
                start = pos + 1
                window_start = max(0, pos - n // 4)
                window = lower[window_start:window_start + n]
                score = sum(1 for s in stems if s in window)
                if score > best_score or (score == best_score and window_start < best_start):
                    best_start, best_score = window_start, score
    if best_start:
        # Start on a word boundary rather than mid-word.
        space = body.find(" ", best_start, best_start + 20)
        if space >= 0:
            best_start = space + 1
    piece = body[best_start:best_start + n]
    return (("…" if best_start else "") + piece
            + ("…" if best_start + n < len(body) else ""))


def human_ask(text: str) -> str | None:
    """What a person (or the delegating agent) actually asked in this turn.

    None when the turn is pure machinery: a compaction summary, a desktop
    event, a heartbeat. A side-session brief yields its Task part."""
    if not text:
        return None
    if _COMPACTION.match(text):
        return None
    cleaned = _EVENT_BLOCK.sub("", text)
    cleaned = _PASTED_TAG.sub("", cleaned).strip()
    if cleaned.startswith("<heartbeat>"):
        return None
    if cleaned.startswith(_BRIEF):
        task = _BRIEF_TASK.search(cleaned)
        if task:
            cleaned = cleaned[task.end():].strip()
    return cleaned or None
