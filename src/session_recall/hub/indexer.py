"""Turn everything members have uploaded into one searchable index.

This is where the hub's "no second implementation" rule pays off: it walks
each member's tree and hands it to the ordinary `index_corpus`, the same
function a laptop runs against its own `~/.claude/projects`. Incremental
signatures, vector reuse for unchanged chunks, pruning of deleted transcripts
and the embedding-space fingerprint all come along for free.

Runs from a timer rather than from the ingest request: an upload should
return as soon as the bytes are durable, and embedding a freshly arrived
month of history behind a POST would time out. One run at a time, enforced by
a lock — two indexers would fight over the same per-file transactions.
"""

import sys
from pathlib import Path

from ..embed import make_embedder
from ..index import index_corpus
from ..metadocs.lock import acquire_lock
from ..store import Store
from . import storage

LOCK_NAME = "hub-index.lock"


def index_all(hub, embedder=None) -> dict:
    """Index every member's transcripts. Returns per-owner new-chunk counts.

    A member whose tree fails to index does not stop the others: their history
    is separate data, and one corrupt transcript should not freeze the team's
    index at yesterday.
    """
    fd = acquire_lock(hub.root, LOCK_NAME)
    if fd is None:
        return {"skipped": "another indexer run holds the lock"}
    counts: dict[str, object] = {}
    store = None
    try:
        store = Store(hub.db_path)
        store.db.execute("PRAGMA journal_mode=WAL")
        store.db.execute("PRAGMA busy_timeout=30000")
        embedder = embedder or make_embedder()
        for owner in storage.owners(hub.transcripts):
            roots = storage.source_roots(hub.transcripts, owner)
            try:
                counts[owner] = index_corpus(
                    store, embedder, roots["claude"], [roots["codex"]])
            except Exception as failure:                   # noqa: BLE001
                counts[owner] = f"failed: {type(failure).__name__}: {failure}"
                print(f"claude-recall: indexing {owner} failed: {failure}",
                      file=sys.stderr)
    finally:
        if store is not None:
            store.close()
        import os
        os.close(fd)
    return counts


def owner_of(hub, file_path: str) -> str | None:
    """Which member a stored transcript belongs to.

    Attribution comes from the path rather than a column: the tree layout
    already encodes it, and deriving it keeps the shared schema identical to
    the solo one.
    """
    try:
        rel = Path(file_path).resolve().relative_to(
            Path(hub.transcripts).resolve())
    except (ValueError, OSError):
        return None
    return rel.parts[0] if rel.parts else None
