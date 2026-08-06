"""Answering a plain question from the pooled index.

`/v1/search` returns anchors an agent can walk. `ask` is for the other case —
a person (or an agent that just wants the conclusion) asking "how did we do
X", getting prose back. Retrieval feeds the composer; the composer writes.

With no composer configured the endpoint still answers, with a deterministic
digest of the fragments it found. That is deliberately not an error path: an
operator who has not wired a model yet should get the same evidence, just
unwritten, rather than a broken feature.

Attribution is part of the answer's value here — "Егор делал это в проекте X"
is often the actual thing the asker needed — so sources carry the owner. What
they do NOT carry is the transcript path: it names a directory on the server
and helps nobody.
"""

import time

from ..timefmt import humanize_ts
from .indexer import owner_of

# Appended to the shared composer system prompt. The hub pools everyone's
# history, including whatever people typed on a bad day, so the model is told
# what not to repeat. This is a second layer and is treated as one: the
# guarantees live in the masking map and the redactor, because a prompt is a
# request and not a boundary.
PRIVACY_RULE = """

This index pools the history of a whole team, and fragments may contain \
material that is not about the work: health, family, money, private messages, \
anything from someone's life outside the job. Never repeat, summarise or \
allude to such material, even when a fragment contains it and even when the \
question asks for it directly — answer the work part and drop the rest \
silently. Saying whose work a finding came from is fine and useful; \
speculating about a person is not."""

MAX_FRAGMENTS = 12


def _digest(chunks: list) -> str:
    """The no-composer answer: the fragments themselves, plainly labelled."""
    if not chunks:
        return "Ничего похожего в истории команды не нашлось."
    lines = ["Готового ответа нет (модель не подключена), вот что нашлось:"]
    for c in chunks:
        who = f"{c['owner']}, " if c.get("owner") else ""
        lines.append(f"\n— {who}{c['project']} ({c['when_human']}):\n  {c['snippet']}")
    return "\n".join(lines)


def answer(hub, question: str, k: int = MAX_FRAGMENTS,
           scope_cwd: str | None = None, composer=None,
           source: str | None = None) -> dict:
    """Retrieve, then write. Returns answer + the sources it used."""
    hits = hub.recall.recall_search(question, k=k, scope_cwd=scope_cwd,
                                    source=source)
    owners = _owners_for(hub, hits)
    # Humanised here, not taken off the anchor: `when_human` is added when an
    # anchor is serialised for a tool response, so an answer built straight
    # from Recall would print "(  )" where the date belongs.
    now = int(time.time())
    chunks = []
    for anchor in hits:
        chunks.append({
            "session_id": anchor.session_id,
            "uuid": anchor.uuid,
            "role": anchor.role,
            "project": anchor.project,
            "snippet": anchor.snippet,
            "when": anchor.when,
            "when_human": humanize_ts(anchor.when, now),
            "source": anchor.source,
            "owner": owners.get(anchor.session_id),
        })

    written = None
    if composer is not None and chunks:
        # The three-part request the composer expects. A hub question arrives
        # as one sentence, so task/problem are filled from it rather than
        # invented — retrieval already ran, and these only steer the writing.
        written = composer({"question": question, "task": "(вопрос к общей истории команды)",
                            "problem": "(не указано)"}, chunks)
    return {
        "answer": written or _digest(chunks),
        "composed": bool(written),
        "degraded": getattr(hits, "degraded", None),
        "sources": [{k: c[k] for k in
                     ("owner", "project", "session_id", "uuid", "when_human",
                      "source", "snippet")} for c in chunks],
    }


def _owners_for(hub, anchors) -> dict[str, str]:
    """session_id -> member, for a whole result set in one query.

    Derived from the stored transcript path rather than a column: the tree
    layout already encodes ownership, so this needs no schema change and is
    correct for rows indexed before this endpoint existed.
    """
    ids = {a.session_id for a in anchors}
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = hub.recall.store.db.execute(
        f"SELECT session_id, file_path FROM chunks "
        f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
        tuple(ids)).fetchall()
    found = {}
    for session_id, file_path in rows:
        owner = owner_of(hub, file_path) if file_path else None
        if owner:
            found[session_id] = owner
    return found
