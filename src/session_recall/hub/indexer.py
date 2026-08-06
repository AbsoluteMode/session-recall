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


def remask_all(hub) -> dict:
    """Re-apply the CURRENT masking map to everything already stored.

    Masking happens at ingest, so anything uploaded before a map was fixed or
    extended keeps whatever was masked at the time. Re-uploading gigabytes to
    fix that is absurd when the bytes are already here; this rewrites them in
    place instead.

    The ledger is deliberately NOT touched: it counts bytes of the CLIENT's
    file, which this does not change. Rewriting a transcript does change its
    mtime and size, so the next index run re-reads it — and unchanged chunks
    keep their vectors through the content-hash cache, so re-embedding costs
    only what actually changed.
    """
    smap = hub.secret_map
    if not smap:
        return {"skipped": "no masking map configured"}
    changed, masked_total = 0, 0
    for owner in storage.owners(hub.transcripts):
        base = storage.owner_root(hub.transcripts, owner)
        for path in base.rglob("*.jsonl"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="surrogateescape")
                masked, hits = smap.mask(text)
                if not hits:
                    continue
                # Write through a temp file in the same directory: a crash
                # mid-rewrite must not leave a half-masked transcript.
                tmp = path.with_suffix(".jsonl.remask")
                tmp.write_text(masked, encoding="utf-8", errors="surrogateescape")
                tmp.replace(path)
                changed += 1
                masked_total += hits
            except OSError as failure:
                print(f"claude-recall: remask failed for {path}: {failure}",
                      file=sys.stderr)
    return {"files_rewritten": changed, "secrets_masked": masked_total}


def owners_for(hub, session_ids) -> dict[str, str]:
    """session_id -> member, for a whole result set in one query.

    Derived from the stored transcript path rather than a column: the tree
    layout already encodes ownership, so this needs no schema change and is
    correct for rows indexed before attribution existed.
    """
    ids = set(session_ids)
    store = getattr(hub.recall, "store", None)
    if not ids or store is None:
        # Attribution is an enrichment, not the answer. A backend that exposes
        # no store still returns hits — unlabelled beats a failed search.
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = store.db.execute(
        f"SELECT session_id, file_path FROM chunks "
        f"WHERE session_id IN ({placeholders}) GROUP BY session_id",
        tuple(ids)).fetchall()
    found = {}
    for session_id, file_path in rows:
        owner = owner_of(hub, file_path) if file_path else None
        if owner:
            found[session_id] = owner
    return found


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
