"""Feed meta docs entries into the main search index as source="metadocs".

This is the retrieval half of the design: within a project an agent reads the
entry files whole (they are small by construction), but across projects —
«где я уже чинил похожий баг?» — search must work, and the entries are ideal
retrieval chunks: deduplicated, structured, provenance-carrying. They join
the same vector+FTS index the transcripts live in, so `recall_search` and the
MCP tools find them with zero new machinery; `source="metadocs"` filters.

Incremental exactly like transcripts: one entry file = one indexed unit,
keyed by mtime/size signature with the embed fingerprint baked in; edits
re-embed one entry, deletions fall out via prune (path gone from disk).
"""

import hashlib
import time
from pathlib import Path

from ..models import Chunk
from ..store import Store
from . import entries

_SIG_TAG = "metadocs-v1"


def _embed_fp() -> str:
    from .. import config
    return f"{config.EMBED_PROVIDER}/{config.EMBED_MODEL}/{config.EMBED_DIM}"


def _entry_files(repo: Path):
    yield from repo.glob(f"{entries.USER_DIR}/*.md")
    for category in ("bugs", "actions", "decisions"):
        yield from repo.glob(f"*/{category}/*.md")


def _ts(date_str: str) -> int:
    try:
        return int(time.mktime(time.strptime(date_str, "%Y-%m-%d")))
    except (ValueError, OverflowError):
        return int(time.time())


def _chunk(entry: entries.Entry, path: Path, text: str) -> Chunk:
    return Chunk(
        session_id=entry.id, uuid=entry.id, role="doc", text=text,
        project=entry.project or "user-map", cwd="", git_branch="",
        ts=_ts(entry.updated or entry.created), file_path=str(path),
        byte_offset=0, byte_len=len(text.encode()), turn_index=0,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        source="metadocs")


def index_metadocs(store: Store, embedder, repo: Path) -> int:
    """Index changed entries; returns how many were (re)indexed."""
    if not repo.is_dir():
        return 0
    count = 0
    for path in sorted(_entry_files(repo)):
        st = path.stat()
        sig = f"{_SIG_TAG}:{_embed_fp()}:{int(st.st_mtime)}:{st.st_size}"
        if store.is_indexed(str(path), sig):
            continue
        entry = entries.parse(path.read_text())
        if entry is None:
            continue          # half-written or foreign file: skip, no marker
        text = f"{entry.title}\n\n{entry.body}"
        cached = store.embeddings_by_hash(str(path))
        chunk = _chunk(entry, path, text)
        try:
            vec = cached.get(chunk.content_hash)
            if vec is None:
                (vec,) = embedder.embed_documents([text])
            store.delete_file(str(path))
            store.add(chunk, vec)
            store.mark_indexed(str(path), sig, source="metadocs")
            store.commit()
            count += 1
        except Exception:
            store.rollback()
            raise
    store.prune_deleted(source="metadocs")
    return count
