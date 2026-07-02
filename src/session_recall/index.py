import sys
from pathlib import Path
from . import config
from .extract import extract_file, EXTRACTOR_VERSION
from .store import Store
from .embed import Embedder

def _embed_fp() -> str:
    # Which embedding space the vectors live in. Same-dim provider/model swaps
    # produce incompatible spaces, so this must invalidate files AND the reuse
    # cache. WHY: docs/decisions/2026-07-02-post-review-hardening.md
    return f"{config.EMBED_PROVIDER}/{config.EMBED_MODEL}/{config.EMBED_DIM}"

def _file_sig(path: Path) -> str:
    st = path.stat()
    # Extractor version and embed fingerprint are part of the signature: bumping
    # either invalidates every file, so a changed extractor OR embedding model
    # triggers a clean re-extract/re-embed on the next run.
    return f"v{EXTRACTOR_VERSION}:{_embed_fp()}:{int(st.st_mtime)}:{st.st_size}"

def _project_name(project_dir: Path) -> str:
    # "-Users-me-proj" -> "proj" (last path segment of the decoded dir)
    return project_dir.name.lstrip("-").split("-")[-1]

def index_corpus(store: Store, embedder: Embedder, projects_dir: Path) -> int:
    # Drop rows for transcripts deleted since the last run before scanning: a
    # deleted file is never visited below (we only walk existing files), so its
    # chunks would otherwise linger in the index forever.
    store.prune_deleted()
    # Grandfather pre-fingerprint sigs (v{N}:mtime:size — exactly 3 parts): their
    # vectors were produced by the then-configured provider, so stamping the
    # CURRENT fingerprint keeps them valid without a wholesale re-embed of the
    # corpus. A real provider change after this upgrade still mismatches the sig.
    for path, sig in store.db.execute("SELECT path, sig FROM indexed_files").fetchall():
        parts = sig.split(":")
        if len(parts) == 3:
            store.db.execute("UPDATE indexed_files SET sig = ? WHERE path = ?",
                             (f"{parts[0]}:{_embed_fp()}:{parts[1]}:{parts[2]}", path))
    store.commit()
    new_count = 0
    failed: list[str] = []
    for project_dir in sorted(Path(projects_dir).iterdir()):
        if not project_dir.is_dir():
            continue
        project = _project_name(project_dir)
        # Non-recursive on purpose: the flat *.jsonl files ARE the real
        # conversation transcripts. Subagent sidechains live one level down in
        # <session>/subagents/agent-*.jsonl and are intentionally skipped — they
        # are under-the-hood tool/agent internals, not user<->assistant turns,
        # so indexing them would add noise (and ~8x cost) for no recall gain.
        # Switch to rglob only if subagent recall becomes an explicit goal.
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            # One transaction per file: delete + re-add + mark commit together, so
            # a failure mid-file (embedding API down, broken transcript) rolls back
            # to the previous good state — never a half-indexed hole. And one bad
            # file must not abort the run: log it, retry on the next run (its sig
            # stays unmarked), keep indexing the rest. _file_sig stats the path, so
            # it belongs INSIDE the isolation (broken symlink, delete race).
            try:
                sig = _file_sig(jsonl)
                if store.is_indexed(str(jsonl), sig):
                    continue
                # Transcripts are append-only: reuse the vectors of unchanged chunks
                # (matched by content_hash) and only embed genuinely new texts —
                # otherwise every hook run re-embeds the whole live transcript.
                # Reuse ONLY if the stored rows were embedded in the current space
                # (their sig starts with the current version+fingerprint): checked
                # per file, so even a crashed mid-upgrade run can never resurrect
                # old-space blobs. WHY: docs/decisions/2026-07-02-post-review-hardening.md
                stored = store.stored_sig(str(jsonl)) or ""
                same_space = stored.startswith(f"v{EXTRACTOR_VERSION}:{_embed_fp()}:")
                cached = store.embeddings_by_hash(str(jsonl)) if same_space else {}
                # Changed file (or version bump): drop stale rows before re-adding so
                # a growing transcript never accumulates duplicate chunks. No-op if new.
                store.delete_file(str(jsonl))
                chunks = extract_file(str(jsonl), project=project)
                if chunks:
                    new_texts = [c.text for c in chunks if c.content_hash not in cached]
                    vecs = embedder.embed_documents(new_texts) if new_texts else []
                    if len(vecs) != len(new_texts):
                        # a bare StopIteration would log an empty cause line
                        raise RuntimeError(
                            f"embedder returned {len(vecs)} vectors for {len(new_texts)} texts")
                    new_vecs = iter(vecs)
                    for chunk in chunks:
                        reused = cached.get(chunk.content_hash)
                        store.add(chunk, reused if reused is not None else next(new_vecs))
                store.mark_indexed(str(jsonl), sig)
                store.commit()
                new_count += len(chunks)
            except Exception as e:
                store.rollback()
                failed.append(f"{jsonl}: {e}")
    if failed:
        print(f"session-recall: {len(failed)} file(s) failed to index (will retry "
              f"next run):\n  " + "\n  ".join(failed[:10]), file=sys.stderr)
    return new_count
