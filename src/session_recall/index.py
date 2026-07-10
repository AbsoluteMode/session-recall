import sys
from pathlib import Path
from collections.abc import Iterable
from . import config
from .extract import extract_file, extractor_tag
from .store import Store
from .embed import Embedder
from .scope import project_label
from .transcripts import codex_file_header

def _embed_fp() -> str:
    # Which embedding space the vectors live in. Same-dim provider/model swaps
    # produce incompatible spaces, so this must invalidate files AND the reuse
    # cache. WHY: docs/decisions/2026-07-02-post-review-hardening.md
    return f"{config.EMBED_PROVIDER}/{config.EMBED_MODEL}/{config.EMBED_DIM}"

def _file_sig(path: Path, source: str = "claude") -> str:
    st = path.stat()
    # Extractor version and embed fingerprint are part of the signature: bumping
    # either invalidates every file, so a changed extractor OR embedding model
    # triggers a clean re-extract/re-embed on the next run.
    prefix = f"{extractor_tag(source)}:{_embed_fp()}"
    if source == "codex":
        # Codex rollouts can move into the archive or be replaced. inode makes
        # same-size/same-second replacements visible; path moves still reuse
        # vectors by stable session id.
        return f"{prefix}:{st.st_ino}:{int(st.st_mtime)}:{st.st_size}"
    # Preserve the established Claude signature so this upgrade does not
    # invalidate and re-embed the existing corpus.
    return f"{prefix}:{int(st.st_mtime)}:{st.st_size}"

def _project_name(project_dir: Path) -> str:
    # "-Users-me-proj" -> "proj" (last path segment of the decoded dir)
    return project_dir.name.lstrip("-").split("-")[-1]

def _claude_files(projects_dir: Path | None):
    if projects_dir is None or not Path(projects_dir).is_dir():
        return
    for project_dir in sorted(Path(projects_dir).iterdir()):
        if not project_dir.is_dir():
            continue
        project = _project_name(project_dir)
        # Non-recursive on purpose: nested files are Claude subagent sidechains.
        for jsonl in sorted(project_dir.glob("*.jsonl")):
            yield jsonl, "claude", project


def _codex_files(codex_dirs: Iterable[Path]):
    seen: set[str] = set()
    for root in codex_dirs:
        root = Path(root)
        if not root.is_dir():
            continue
        # Active sessions are nested YYYY/MM/DD; archives are currently flat.
        # rglob covers both layouts and future-proofs archive organization.
        for jsonl in sorted(root.rglob("*.jsonl")):
            key = str(jsonl)
            if key in seen:
                continue
            seen.add(key)
            yield jsonl, "codex", ""


def _forget_file(store: Store, path: str):
    """Remove a file that is now recognized as an under-the-hood sidechain."""
    store.delete_file(path)
    store.db.execute("DELETE FROM indexed_files WHERE path = ?", (path,))
    store.commit()


def _roots_safe_to_prune(store: Store, roots: Iterable[Path], source: str) -> bool:
    """Do not mistake an unavailable transcript root for mass deletion.

    A root that has never existed is harmless (for example Codex may not have
    created ``archived_sessions`` yet). A missing root that still owns indexed
    paths is treated as temporarily unavailable, so its last good rows remain.
    """
    indexed = [Path(row[0]).expanduser().absolute() for row in store.db.execute(
        "SELECT path FROM indexed_files WHERE source = ?", (source,)
    ).fetchall()]
    return all(
        Path(root).expanduser().absolute().is_dir()
        or not any(
            path.is_relative_to(Path(root).expanduser().absolute())
            for path in indexed
        )
        for root in roots
    )


def index_corpus(store: Store, embedder: Embedder, projects_dir: Path | None,
                 codex_dirs: Iterable[Path] | None = None) -> int:
    """Incrementally index Claude Code plus optional Codex transcript roots.

    The fourth argument is optional to preserve the original Claude-only Python
    API.  The CLI passes both Codex active and archive roots by default.
    """
    codex_roots = tuple(codex_dirs or ())
    selected_sources: set[str] = set()
    prunable_sources: set[str] = set()
    if projects_dir is not None:
        selected_sources.add("claude")
        if Path(projects_dir).is_dir():
            prunable_sources.add("claude")
    if codex_roots:
        selected_sources.add("codex")
        if _roots_safe_to_prune(store, codex_roots, "codex"):
            prunable_sources.add("codex")
    # Grandfather pre-fingerprint sigs (v{N}:mtime:size — exactly 3 parts): their
    # vectors were produced by the then-configured provider, so stamping the
    # CURRENT fingerprint keeps them valid without a wholesale re-embed of the
    # corpus. A real provider change after this upgrade still mismatches the sig.
    for path, sig, source in store.db.execute(
            "SELECT path, sig, source FROM indexed_files").fetchall():
        if source not in selected_sources:
            continue
        parts = sig.split(":")
        if len(parts) == 3 and parts[0].startswith("v") and parts[0][1:].isdigit():
            store.db.execute("UPDATE indexed_files SET sig = ? WHERE path = ?",
                             (f"{parts[0]}:{_embed_fp()}:{parts[1]}:{parts[2]}", path))
    store.commit()
    new_count = 0
    failed: list[str] = []
    failed_sources: set[str] = set()
    jobs = list(_claude_files(projects_dir) or ())
    jobs.extend(_codex_files(codex_roots))
    for jsonl, source, project in jobs:
        # One transaction per file: delete + re-add + marker commit together.
        # _file_sig and header reads stay INSIDE this isolation for delete races,
        # broken symlinks, or partially-written live rollouts.
        try:
            if source == "codex":
                meta = codex_file_header(jsonl)
                if meta.is_sidechain:
                    _forget_file(store, str(jsonl))
                    continue
                project = project_label(meta.cwd) or "codex"
            sig = _file_sig(jsonl, source)
            if store.is_indexed(str(jsonl), sig):
                continue
            # Reuse only vectors produced by the same source extractor version
            # and embedding space. This makes live append-only updates cheap.
            stored = store.stored_sig(str(jsonl)) or ""
            sig_prefix = f"{extractor_tag(source)}:{_embed_fp()}:"
            same_space = stored.startswith(sig_prefix)
            cached = store.embeddings_by_hash(str(jsonl)) if same_space else {}
            # Codex moves completed rollouts from sessions/ to
            # archived_sessions/. Reuse the old path's vectors by stable thread
            # id so archiving does not resend the whole conversation.
            if source == "codex" and not stored and not cached:
                cached = store.embeddings_by_session(
                    meta.session_id, source="codex", sig_prefix=sig_prefix)
            store.delete_file(str(jsonl))
            chunks = extract_file(str(jsonl), project=project, source=source)
            if chunks:
                # A repeated prompt/reply needs one embedding even though every
                # occurrence keeps its own provenance row.
                missing: dict[str, str] = {}
                for chunk in chunks:
                    if chunk.content_hash not in cached:
                        missing.setdefault(chunk.content_hash, chunk.text)
                new_texts = list(missing.values())
                vecs = embedder.embed_documents(new_texts) if new_texts else []
                if len(vecs) != len(new_texts):
                    raise RuntimeError(
                        f"embedder returned {len(vecs)} vectors for {len(new_texts)} texts")
                fresh = dict(zip(missing, vecs))
                for chunk in chunks:
                    reused = cached.get(chunk.content_hash)
                    store.add(chunk, reused if reused is not None else fresh[chunk.content_hash])
            store.mark_indexed(str(jsonl), sig, source=source)
            store.commit()
            new_count += len(chunks)
        except Exception as e:
            store.rollback()
            failed.append(f"{jsonl}: {e}")
            failed_sources.add(source)
    # Prune after successful replacements. If an archive move fails to index,
    # retaining the missing old path's rows is preferable to creating a recall
    # hole; the next clean run reconciles it.
    for source in sorted(prunable_sources - failed_sources):
        store.prune_deleted(source=source)
    if failed:
        print(f"session-recall: {len(failed)} file(s) failed to index (will retry "
              f"next run):\n  " + "\n  ".join(failed[:10]), file=sys.stderr)
    return new_count
