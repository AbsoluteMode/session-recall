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


def test_reindex_reuses_embeddings_for_unchanged_chunks(tmp_path):
    """Transcripts are append-only: a grown file is fully re-extracted, but its
    unchanged chunks must NOT be re-embedded — only genuinely new texts hit the
    embedding API. (Live cost: the top transcript is ~1400 chunks re-embedded on
    every SessionStart hook run without this.)"""
    class _CountingEmbedder(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.texts_seen = []

        def embed_documents(self, texts):
            self.texts_seen.extend(texts)
            return super().embed_documents(texts)

    projects = _corpus(tmp_path)  # session_a: 2 surface chunks
    store = Store(tmp_path / "i.db")
    emb = _CountingEmbedder()
    index_corpus(store, emb, projects)
    assert len(emb.texts_seen) == 2
    f = projects / "-Users-me-proj" / "session_a.jsonl"
    with open(f, "a") as fh:
        fh.write('{"type":"user","uuid":"u9","sessionId":"sa",'
                 '"message":{"role":"user","content":"brand new tail line"}}\n')
    emb.texts_seen.clear()
    added = index_corpus(store, emb, projects)
    assert added == 3  # the whole file is re-extracted (row contract unchanged)
    assert emb.texts_seen == ["brand new tail line"], \
        f"unchanged chunks were re-embedded: {emb.texts_seen}"
    # the reused vectors must be byte-intact: KNN with the old text's exact
    # (deterministic) vector still lands on the old chunk
    qv = FakeEmbedder()._vec("how do we cache embeddings")
    texts = [store.get_chunk(cid).text for cid, _ in store.knn(qv, 3)]
    assert "how do we cache embeddings" in texts
    store.close()


class _PoisonEmbedder(FakeEmbedder):
    """Fails on any batch containing 'poison' — simulates a per-file API failure
    (Voyage 4xx on one oversized/broken transcript)."""
    def embed_documents(self, texts):
        if any("poison" in t for t in texts):
            raise RuntimeError("simulated embedding API failure")
        return super().embed_documents(texts)


def test_index_survives_per_file_embed_failure(tmp_path):
    """One bad file must not abort the whole run: later files still get indexed,
    and the failing file stays unmarked so the next run retries it. (Live risk:
    one bad transcript starved the other 420 files, silently, from a hook.)"""
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    bad = proj / "a_bad.jsonl"  # sorted() visits it FIRST — must not kill the run
    bad.write_text('{"type":"user","uuid":"u1","sessionId":"sb",'
                   '"message":{"role":"user","content":"poison text"}}\n')
    good = proj / "b_good.jsonl"
    good.write_text('{"type":"user","uuid":"u2","sessionId":"sg",'
                    '"message":{"role":"user","content":"healthy text"}}\n')
    store = Store(tmp_path / "i.db")
    n = index_corpus(store, _PoisonEmbedder(), tmp_path / "projects")
    assert n == 1, "the healthy file must be indexed despite the earlier failure"
    assert store.fts("healthy", 5), "good file missing from the index"
    assert store.db.execute("SELECT count(*) FROM indexed_files WHERE path = ?",
                            (str(bad),)).fetchone()[0] == 0, \
        "failed file must stay unmarked so the next run retries it"
    store.close()


def test_model_change_reembeds_everything_with_current_model(tmp_path, monkeypatch):
    """Same-dim provider/model switch must not silently mix vector spaces (KNN
    over mixed spaces ranks garbage). The embed fingerprint is part of every
    file's sig — so a switch invalidates ALL files — and gates the vector-reuse
    cache — so the re-index really re-embeds with the CURRENT model instead of
    resurrecting old-space blobs by content_hash."""
    from session_recall import config

    class _Counting(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.texts_seen = []

        def embed_documents(self, texts):
            self.texts_seen.extend(texts)
            return super().embed_documents(texts)

    projects = _corpus(tmp_path)
    store = Store(tmp_path / "i.db")
    emb = _Counting()
    index_corpus(store, emb, projects)
    assert len(emb.texts_seen) == 2
    emb.texts_seen.clear()

    monkeypatch.setattr(config, "EMBED_MODEL", "other-model-same-dim")
    n = index_corpus(store, emb, projects)  # NO file content changed
    assert n == 2, "fingerprint in sig must invalidate every file on model change"
    assert sorted(emb.texts_seen) == ["We cache via an on-disk store.",
                                      "how do we cache embeddings"], \
        f"cache must be bypassed on fingerprint change, saw: {emb.texts_seen}"
    store.close()


def test_legacy_sig_migrates_without_full_reembed(tmp_path):
    """Pre-fingerprint DBs store sig as v{N}:mtime:size. On the first run after
    the upgrade those are grandfathered to the current fingerprint (their
    vectors were made by the then-configured provider) — NOT re-embedded
    wholesale, which would cost a full-corpus embed for nothing."""
    projects = _corpus(tmp_path)
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    index_corpus(store, emb, projects)  # writes new-format sig
    f = str(projects / "-Users-me-proj" / "session_a.jsonl")
    new_sig = store.db.execute(
        "SELECT sig FROM indexed_files WHERE path = ?", (f,)).fetchone()[0]
    legacy = "v2:" + ":".join(new_sig.split(":")[-2:])  # v2:mtime:size
    store.db.execute("UPDATE indexed_files SET sig = ? WHERE path = ?", (legacy, f))
    store.commit()
    calls = emb.doc_calls
    assert index_corpus(store, emb, projects) == 0
    assert emb.doc_calls == calls, "legacy sig must migrate in place, not re-embed"
    store.close()


def test_index_survives_unstatable_file(tmp_path):
    """A transcript that vanishes between glob and stat (broken symlink, race
    with cleanup) must not abort the run: _file_sig stats the path, so it must
    fail inside the per-file isolation, not before it."""
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    (proj / "a_broken.jsonl").symlink_to(proj / "never-existed-target.jsonl")
    good = proj / "b_good.jsonl"
    good.write_text('{"type":"user","uuid":"u2","sessionId":"sg",'
                    '"message":{"role":"user","content":"healthy text"}}\n')
    store = Store(tmp_path / "i.db")
    n = index_corpus(store, FakeEmbedder(), tmp_path / "projects")
    assert n == 1, "the healthy file must be indexed despite the unstatable one"
    assert store.fts("healthy", 5)
    store.close()


def test_failed_reindex_keeps_previous_chunks(tmp_path):
    """A transcript grows, then its re-embed fails mid-file. The previously
    indexed chunks must survive (per-file transaction rollback) — not vanish
    half-deleted, leaving recall blind on that session until a later run."""
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    f = proj / "s.jsonl"
    f.write_text('{"type":"user","uuid":"u1","sessionId":"sa",'
                 '"message":{"role":"user","content":"original insight"}}\n')
    store = Store(tmp_path / "i.db")
    index_corpus(store, FakeEmbedder(), tmp_path / "projects")
    assert store.fts("original", 5)
    with open(f, "a") as fh:
        fh.write('{"type":"user","uuid":"u2","sessionId":"sa",'
                 '"message":{"role":"user","content":"poison appended"}}\n')
    index_corpus(store, _PoisonEmbedder(), tmp_path / "projects")  # re-embed fails
    assert store.fts("original", 5), \
        "previous chunks must survive a failed re-index (rollback, not a hole)"
    store.close()


def test_short_embedder_batch_fails_with_actionable_message(tmp_path, capsys):
    """An embedder returning fewer vectors than texts must fail that file with a
    real message in the unattended-hook log — a bare StopIteration stringifies
    to '' and the stderr line would read 'path: ' with no cause."""
    class _ShortEmbedder(FakeEmbedder):
        def embed_documents(self, texts):
            return super().embed_documents(texts)[:-1]  # one vector short

    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text('{"type":"user","uuid":"u1","sessionId":"sa",'
                                  '"message":{"role":"user","content":"some text"}}\n')
    store = Store(tmp_path / "i.db")
    n = index_corpus(store, _ShortEmbedder(), tmp_path / "projects")
    assert n == 0
    err = capsys.readouterr().err
    assert "1 file(s) failed" in err
    assert "vector" in err, f"log line must name the cause, got: {err!r}"
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
