from pathlib import Path
from .extract import extract_file, EXTRACTOR_VERSION
from .store import Store
from .embed import Embedder

def _file_sig(path: Path) -> str:
    st = path.stat()
    # Extractor version is part of the signature: bumping it invalidates every
    # file so a changed extractor triggers a clean re-index on the next run.
    return f"v{EXTRACTOR_VERSION}:{int(st.st_mtime)}:{st.st_size}"

def _project_name(project_dir: Path) -> str:
    # "-Users-me-proj" -> "proj" (last path segment of the decoded dir)
    return project_dir.name.lstrip("-").split("-")[-1]

def index_corpus(store: Store, embedder: Embedder, projects_dir: Path) -> int:
    # Drop rows for transcripts deleted since the last run before scanning: a
    # deleted file is never visited below (we only walk existing files), so its
    # chunks would otherwise linger in the index forever.
    store.prune_deleted()
    new_count = 0
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
            sig = _file_sig(jsonl)
            if store.is_indexed(str(jsonl), sig):
                continue
            # Changed file (or version bump): drop stale rows before re-adding so a
            # growing transcript never accumulates duplicate chunks. No-op if new.
            store.delete_file(str(jsonl))
            chunks = extract_file(str(jsonl), project=project)
            if chunks:
                vectors = embedder.embed_documents([c.text for c in chunks])
                for chunk, vec in zip(chunks, vectors):
                    store.add(chunk, vec)
                new_count += len(chunks)
            store.mark_indexed(str(jsonl), sig)
    return new_count
