import shutil
from pathlib import Path
from session_recall.store import Store
from session_recall.embed import FakeEmbedder
from session_recall.index import index_corpus

def _corpus(tmp_path) -> Path:
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    shutil.copy("tests/fixtures/session_a.jsonl", proj / "session_a.jsonl")
    return tmp_path / "projects"

def test_index_then_incremental_noop(tmp_path):
    projects = _corpus(tmp_path)
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    first = index_corpus(store, emb, projects)
    assert first == 2  # 2 surface chunks in fixture
    calls_after_first = emb.doc_calls
    second = index_corpus(store, emb, projects)
    assert second == 0  # nothing changed -> no re-embed
    assert emb.doc_calls == calls_after_first  # embedder NOT re-invoked
    store.close()

def test_changed_file_reindexes(tmp_path):
    projects = _corpus(tmp_path)
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    index_corpus(store, emb, projects)
    f = projects / "-Users-me-proj" / "session_a.jsonl"
    with open(f, "a") as fh:
        fh.write('{"type":"user","uuid":"u9","sessionId":"sa","message":{"role":"user","content":"new line"}}\n')
    added = index_corpus(store, emb, projects)
    assert added == 3  # whole file re-extracted after change (3 surface chunks now)
    store.close()


def test_index_prunes_chunks_for_deleted_transcripts(tmp_path):
    """A transcript indexed once, then deleted from disk, must have its chunks
    pruned on the next index run. index_corpus only walks EXISTING files, so a
    deleted file is never re-visited — without an explicit prune its chunks would
    linger forever, polluting recall_search (and, pre-resilience, crashing grep)."""
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    keep = proj / "keep.jsonl"
    keep.write_text('{"type":"user","uuid":"u1","sessionId":"sa",'
                    '"message":{"role":"user","content":"alpha survives"}}\n')
    gone = proj / "gone.jsonl"
    gone.write_text('{"type":"user","uuid":"u2","sessionId":"sb",'
                    '"message":{"role":"user","content":"beta removed"}}\n')
    store = Store(tmp_path / "p.db")
    emb = FakeEmbedder()
    index_corpus(store, emb, tmp_path / "projects")
    assert store.db.execute("SELECT count(*) FROM chunks WHERE file_path = ?",
                            (str(gone),)).fetchone()[0] == 1  # indexed

    gone.unlink()  # session cleaned up after indexing
    index_corpus(store, emb, tmp_path / "projects")

    assert store.db.execute("SELECT count(*) FROM chunks WHERE file_path = ?",
                            (str(gone),)).fetchone()[0] == 0, "deleted file's chunks not pruned"
    assert store.db.execute("SELECT count(*) FROM chunks WHERE file_path = ?",
                            (str(keep),)).fetchone()[0] == 1, "live file's chunks wrongly removed"
    assert store.db.execute("SELECT count(*) FROM indexed_files WHERE path = ?",
                            (str(gone),)).fetchone()[0] == 0, "indexed_files row for deleted file left behind"
    # vec/fts stay in sync with chunks (no orphan rows)
    assert store.db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 1
    assert store.db.execute("SELECT count(*) FROM fts_chunks").fetchone()[0] == 1
    store.close()


def test_reindex_changed_file_does_not_accumulate_duplicate_rows(tmp_path):
    """Delete-before-reinsert: a growing transcript re-indexed must NOT leave the
    old chunks behind. The DB must hold only the current file's chunks (3), not 5."""
    projects = _corpus(tmp_path)
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    index_corpus(store, emb, projects)
    f = projects / "-Users-me-proj" / "session_a.jsonl"
    with open(f, "a") as fh:
        fh.write('{"type":"user","uuid":"u9","sessionId":"sa","message":{"role":"user","content":"new line"}}\n')
    index_corpus(store, emb, projects)
    total = store.db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    vec = store.db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
    fts = store.db.execute("SELECT count(*) FROM fts_chunks").fetchone()[0]
    assert total == 3, f"duplicate accumulation: {total} chunk rows (expected 3)"
    assert vec == 3 and fts == 3, f"vec/fts out of sync with chunks: vec={vec} fts={fts}"
    store.close()
