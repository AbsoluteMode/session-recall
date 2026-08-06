from session_recall.store import Store
from session_recall.models import Chunk
import sqlite3

def _chunk(uuid, text):
    return Chunk(session_id="s", uuid=uuid, role="user", text=text, project="p",
                 cwd="/c", git_branch="b", ts=1, file_path="/f.jsonl",
                 byte_offset=0, byte_len=5, turn_index=0, content_hash=uuid)

def test_add_and_knn(tmp_path):
    s = Store(tmp_path / "t.db")
    a = s.add(_chunk("u1", "alpha"), [1.0] + [0.0] * 1023)
    b = s.add(_chunk("u2", "beta"), [0.0, 1.0] + [0.0] * 1022)
    hits = s.knn([1.0] + [0.0] * 1023, n=2)
    assert hits[0][0] == a  # nearest is u1
    assert hits[1][0] == b
    assert isinstance(hits[0][1], float)
    assert hits[0][1] < hits[1][1]
    s.close()

def test_fts_and_get_chunk(tmp_path):
    s = Store(tmp_path / "t.db")
    cid = s.add(_chunk("u1", "embedding cache strategy"), [0.0] * 1024)
    assert s.fts("embedding", n=5) == [cid]
    assert s.get_chunk(cid).uuid == "u1"
    s.close()


def test_date_range_prefilters_knn_fts_and_recent_sessions(tmp_path):
    """Date constraints must reach sqlite-vec, FTS, and aggregation before LIMIT."""
    s = Store(tmp_path / "dated.db")
    old = _chunk("old", "daily memory")
    old.ts = 100
    old.session_id = "s-old"
    fresh = _chunk("fresh", "daily memory")
    fresh.ts = 200
    fresh.session_id = "s-fresh"
    unknown = _chunk("unknown", "daily memory")
    unknown.ts = 0
    unknown.session_id = "s-unknown"
    vector = [1.0] + [0.0] * 1023
    s.add(old, vector)
    fresh_id = s.add(fresh, vector)
    s.add(unknown, vector)

    assert [cid for cid, _ in s.knn(
        vector, n=10, start_ts=150, end_ts=250)] == [fresh_id]
    assert s.fts("daily", n=10, start_ts=150, end_ts=250) == [fresh_id]
    recent = s.recent_sessions(None, 10, start_ts=150, end_ts=250)
    assert [(row[1], row[4]) for row in recent] == [("s-fresh", 1)]

    # An upper-only range must not classify unknown ts=0 as old evidence.
    assert {row[1] for row in s.recent_sessions(None, 10, end_ts=150)} == {"s-old"}
    s.close()

def test_indexed_marker(tmp_path):
    s = Store(tmp_path / "t.db")
    assert not s.is_indexed("/f.jsonl", "sig1")
    s.mark_indexed("/f.jsonl", "sig1")
    assert s.is_indexed("/f.jsonl", "sig1")
    assert not s.is_indexed("/f.jsonl", "sig2")  # changed signature
    s.close()


def test_source_provenance_defaults_and_filters(tmp_path):
    s = Store(tmp_path / "sources.db")
    claude = _chunk("c1", "shared memory from claude")
    codex = _chunk("x1", "shared memory from codex")
    codex.source = "codex"
    c_id = s.add(claude, [1.0] + [0.0] * 1023)
    x_id = s.add(codex, [0.0, 1.0] + [0.0] * 1022)

    assert s.get_chunk(c_id).source == "claude"
    assert s.get_chunk(x_id).source == "codex"
    assert [cid for cid, _ in s.knn([1.0] + [0.0] * 1023, 5,
                                     source="codex")] == [x_id]
    assert s.fts("memory", 5, source="claude") == [c_id]
    assert s.fts("memory", 5, source="codex") == [x_id]
    s.close()


def test_indexed_file_records_source(tmp_path):
    s = Store(tmp_path / "sources.db")
    s.mark_indexed("/codex.jsonl", "sig", source="codex")
    assert s.indexed_source("/codex.jsonl") == "codex"
    s.close()


def test_existing_claude_database_migrates_source_columns_in_place(tmp_path):
    """The shared-source upgrade must preserve a v0.2 Claude-only database."""
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE chunks(id INTEGER PRIMARY KEY, session_id TEXT, uuid TEXT, "
        "role TEXT, text TEXT, project TEXT, cwd TEXT, git_branch TEXT, ts INTEGER, "
        "file_path TEXT, byte_offset INTEGER, byte_len INTEGER, turn_index INTEGER, "
        "content_hash TEXT)"
    )
    db.execute(
        "INSERT INTO chunks(session_id, uuid, role, text, project, cwd, git_branch, ts, "
        "file_path, byte_offset, byte_len, turn_index, content_hash) "
        "VALUES ('s', 'u', 'user', 'legacy text', 'p', '/p', '', 1, '/old.jsonl', 0, 1, 0, 'h')"
    )
    db.execute("CREATE TABLE indexed_files(path TEXT PRIMARY KEY, sig TEXT)")
    db.execute("INSERT INTO indexed_files VALUES ('/old.jsonl', 'v2:1:1')")
    db.commit()
    db.close()

    store = Store(path)
    assert store.db.execute("SELECT source FROM chunks").fetchone()[0] == "claude"
    assert store.indexed_source("/old.jsonl") == "claude"
    assert store.get_chunk(1).source == "claude"
    store.close()


def test_knn_source_filter_cannot_starve_a_small_source(tmp_path):
    """A small Codex corpus must remain searchable even when hundreds of
    closer Claude vectors precede it in the global KNN order."""
    s = Store(tmp_path / "starvation.db")
    close = [1.0] + [0.0] * 1023
    far = [0.0, 1.0] + [0.0] * 1022
    for i in range(305):
        s.add(_chunk(f"c{i}", f"claude {i}"), close)
    codex = _chunk("codex-only", "rare codex memory")
    codex.source = "codex"
    codex_id = s.add(codex, far)

    hits = s.knn(close, n=1, source="codex")
    assert hits and hits[0][0] == codex_id
    s.close()

def test_fts_or_join_non_adjacent_terms(tmp_path):
    """Regression for I2: fts("drop design", …) must match a chunk that contains
    both words but NOT as a consecutive phrase.  Under the old phrase-match
    implementation ("drop design") this would return nothing."""
    s = Store(tmp_path / "t.db")
    cid = s.add(_chunk("u1", "resilient drop delivery design"), [0.0] * 1024)
    hits = s.fts("drop design", 5)
    assert cid in hits, "OR-join FTS failed to match non-adjacent terms"
    s.close()

def test_fts_empty_query_returns_empty(tmp_path):
    """Empty query must short-circuit to [] without hitting SQLite."""
    s = Store(tmp_path / "t.db")
    s.add(_chunk("u1", "some text"), [0.0] * 1024)
    assert s.fts("", 5) == []
    s.close()

def test_fts_ranks_by_bm25_within_limit(tmp_path):
    """fts() must return the BEST bm25 matches within n, not the first-inserted
    rows. FTS5 without ORDER BY yields rowid (insertion) order, so LIMIT keeps an
    arbitrary oldest slice and starves the hybrid's keyword arm of its actual
    best hits (live index: 1158 matches, LIMIT 100 kept a random 8%)."""
    s = Store(tmp_path / "t.db")
    s.add(_chunk("u1", "embedding " + "unrelated filler words " * 40), [0.0] * 1024)
    strong = s.add(_chunk("u2", "embedding cache embedding strategy embedding"), [0.0] * 1024)
    assert s.fts("embedding", n=1) == [strong], \
        "expected the dense bm25 match, got the first-inserted row"
    s.close()

def test_fts_scoped_ranks_by_bm25_within_limit(tmp_path):
    """Same bm25 ordering guarantee for the scoped (JOIN) branch of fts()."""
    s = Store(tmp_path / "t.db")
    s.add(_chunk_cwd("u1", "embedding " + "unrelated filler words " * 40, "/repo/x"), [0.0] * 1024)
    strong = s.add(_chunk_cwd("u2", "embedding cache embedding strategy embedding", "/repo/y"),
                   [0.0] * 1024)
    assert s.fts("embedding", n=1, scope_root="/repo") == [strong], \
        "scoped fts must also rank by bm25, not insertion order"
    s.close()


def _chunk_in(uuid, text, file_path):
    c = _chunk(uuid, text)
    c.file_path = file_path
    return c


def _chunk_cwd(uuid, text, cwd):
    c = _chunk(uuid, text)
    c.cwd = cwd
    return c


def test_knn_scope_filters_by_cwd_prefix(tmp_path):
    """knn(scope_root=...) keeps only chunks whose cwd is at/under the root —
    including worktrees nested under it."""
    s = Store(tmp_path / "t.db")
    inside = s.add(_chunk_cwd("u1", "alpha", "/repo/.claude/worktrees/wt-1"), [1.0] + [0.0] * 1023)
    s.add(_chunk_cwd("u2", "alpha two", "/other/proj"), [0.9, 0.1] + [0.0] * 1022)
    hits = s.knn([1.0] + [0.0] * 1023, n=5, scope_root="/repo")
    ids = [cid for cid, _ in hits]
    assert inside in ids
    assert all(s.get_chunk(cid).cwd.startswith("/repo") for cid in ids)
    s.close()


def test_knn_scope_excludes_sibling_prefix(tmp_path):
    """Boundary: scope '/repo' must NOT match the sibling '/repo-backend'."""
    s = Store(tmp_path / "t.db")
    s.add(_chunk_cwd("u1", "alpha", "/repo-backend/src"), [1.0] + [0.0] * 1023)
    assert s.knn([1.0] + [0.0] * 1023, n=5, scope_root="/repo") == []
    s.close()


def test_fts_scope_filters_by_cwd_prefix(tmp_path):
    s = Store(tmp_path / "t.db")
    inside = s.add(_chunk_cwd("u1", "embedding cache", "/repo/sub"), [0.0] * 1024)
    s.add(_chunk_cwd("u2", "embedding cache", "/other"), [0.0] * 1024)
    assert s.fts("embedding", n=5, scope_root="/repo") == [inside]
    s.close()


def test_fts_scope_excludes_sibling_prefix(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add(_chunk_cwd("u1", "embedding cache", "/repo-backend"), [0.0] * 1024)
    assert s.fts("embedding", n=5, scope_root="/repo") == []
    s.close()


def test_knn_and_fts_skip_orphaned_index_rows(tmp_path):
    """A vec/fts row whose chunk row is gone must never surface as a candidate.
    Live repro: 120 orphans in the production index crashed every unscoped
    recall_search with TypeError: 'NoneType' object is not iterable (get_chunk on
    a missing id). Scoped queries JOIN chunks and were immune; unscoped ones read
    the index tables straight and handed the dangling id to get_chunk."""
    s = Store(tmp_path / "orphan.db")
    ghost = s.add(_chunk("u1", "alpha ghost"), [1.0] + [0.0] * 1023)
    alive = s.add(_chunk("u2", "alpha alive"), [0.9] + [0.0] * 1023)
    # The desync itself: chunk row vanished, its index rows lingered.
    s.db.execute("DELETE FROM chunks WHERE id = ?", (ghost,))
    s.db.commit()

    assert [cid for cid, _ in s.knn([1.0] + [0.0] * 1023, 5)] == [alive]
    assert s.fts("alpha", 5) == [alive]
    s.close()


def test_delete_file_removes_chunks_vec_and_fts(tmp_path):
    """delete_file must clear a file's rows from chunks + vec_chunks + fts_chunks,
    leaving other files untouched (used for delete-before-reinsert)."""
    s = Store(tmp_path / "t.db")
    s.add(_chunk_in("u1", "alpha one", "/a.jsonl"), [1.0] + [0.0] * 1023)
    s.add(_chunk_in("u2", "alpha two", "/a.jsonl"), [0.0, 1.0] + [0.0] * 1022)
    keep = s.add(_chunk_in("u3", "beta survivor", "/b.jsonl"), [0.0, 0.0, 1.0] + [0.0] * 1021)
    s.delete_file("/a.jsonl")
    assert s.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
    assert s.db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 1
    assert s.fts("alpha", 5) == []  # deleted content gone from FTS
    assert [cid for cid, _ in s.knn([1.0] + [0.0] * 1023, 5)] == [keep]  # only survivor in vec
    assert s.get_chunk(keep).uuid == "u3"
    s.close()


def test_get_chunk_returns_none_for_missing_id(tmp_path):
    """A vanished chunk is a normal outcome (background reindex deletes rows while
    a search is in flight), so the read reports absence instead of exploding on a
    None row inside zip()."""
    s = Store(tmp_path / "t.db")
    cid = s.add(_chunk("u1", "alpha"), [1.0] + [0.0] * 1023)
    assert s.get_chunk(cid).uuid == "u1"
    assert s.get_chunk(cid + 999) is None
    s.close()


def test_delete_file_leaves_no_orphans_when_rows_arrive_mid_delete(tmp_path):
    """delete_file must not outlive its own snapshot.

    It read ids from chunks, then deleted chunks by file_path — two different
    criteria. A second indexer (parallel Claude Code sessions each fire the
    freshness hook) committing rows for the same file in between got its chunks
    row deleted by the file_path sweep while its vec/fts rows survived: orphans.
    Live index: 3 contiguous runs of 2, 13 and 105 such rows."""
    path = tmp_path / "race.db"
    victim = Store(path)
    intruder = Store(path)
    victim.add(_chunk_in("u1", "alpha one", "/f.jsonl"), [1.0] + [0.0] * 1023)
    victim.commit()

    class _Snapshot:  # a cursor read before the intruder committed
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _RacingDb:
        """Real connection; the intruder's commit lands right after the first
        read of chunks — the exact window two indexers hit in production."""

        def __init__(self, real):
            self._real = real
            self._fired = False

        def execute(self, sql, *args):
            if not self._fired and sql.lstrip().upper().startswith("SELECT ID FROM CHUNKS"):
                self._fired = True
                rows = self._real.execute(sql, *args).fetchall()
                intruder.add(_chunk_in("u2", "alpha two", "/f.jsonl"),
                             [0.0, 1.0] + [0.0] * 1022)
                intruder.commit()
                return _Snapshot(rows)
            return self._real.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_db = victim.db
    victim.db = _RacingDb(real_db)
    victim.delete_file("/f.jsonl")
    victim.db = real_db
    victim.commit()

    orphans = victim.db.execute(
        "SELECT count(*) FROM vec_chunks v LEFT JOIN chunks c ON c.id = v.chunk_id "
        "WHERE c.id IS NULL").fetchone()[0]
    assert orphans == 0, f"{orphans} vec rows outlived their chunk"
    assert victim.db.execute(
        "SELECT count(*) FROM fts_chunks f LEFT JOIN chunks c ON c.id = f.chunk_id "
        "WHERE c.id IS NULL").fetchone()[0] == 0
    intruder.close()
    victim.close()


def test_dimension_change_fails_loudly_instead_of_looking_like_a_dead_embedder(tmp_path, monkeypatch):
    """Switching embedding preset changes the vector width, and vec0 tables are
    fixed-width. Without a check the raw sqlite error travels up into recall_search's
    degrade path and is reported as 'embeddings unavailable' — sending the user to
    debug a perfectly healthy embedder."""
    import pytest
    from session_recall import config, store as store_mod
    from session_recall.store import Store

    db = tmp_path / "dim.db"
    Store(db).close()
    monkeypatch.setattr(store_mod, "EMBED_DIM", config.EMBED_DIM // 2)

    with pytest.raises(RuntimeError) as exc:
        Store(db)
    msg = str(exc.value).lower()
    assert "dimension" in msg, "the error must name the actual problem"
    assert "index" in msg, "and must say what to do about it"


def test_corpus_summary_reports_what_was_actually_indexed(tmp_path):
    """`indexed N chunks` tells a new user nothing about what they now have. The
    summary has to answer 'what is in there' — how many sessions, over what span,
    and from which engines, because a shared Claude+Codex history is the whole point
    and is invisible in a chunk count."""
    from session_recall.store import Store, corpus_summary

    def chunk(uuid, session_id, project, ts, source="claude"):
        return Chunk(session_id=session_id, uuid=uuid, role="user", text=uuid,
                     project=project, cwd="/c", git_branch="b", ts=ts,
                     file_path="/f.jsonl", byte_offset=0, byte_len=5, turn_index=0,
                     content_hash=uuid, source=source)

    s = Store(tmp_path / "sum.db")
    day = 86400
    vec = [0.0] * 1024
    s.add(chunk("u1", "s1", "proj-a", 1_700_000_000), vec)
    s.add(chunk("u2", "s1", "proj-a", 1_700_000_000 + day), vec)
    s.add(chunk("u3", "s2", "proj-b", 1_700_000_000 + 2 * day, source="codex"), vec)
    s.db.commit()

    out = corpus_summary(s)
    assert out["chunks"] == 3
    assert out["sessions"] == 2
    assert out["by_source"] == {"claude": 1, "codex": 1}, "sessions per engine, not chunks"
    assert out["span_days"] == 2
    assert out["top_projects"][0] == ("proj-a", 2), "busiest project first, with its chunk count"
    s.close()


def test_read_only_store_searches_but_never_writes(tmp_path):
    """A hub's service reads while its indexer writes. A read-write connection
    from the reader made sqlite-vec drop the writer's vectors ("could not write
    to vector blob"), losing 63 transcripts in one real run."""
    import pytest
    from session_recall.config import EMBED_DIM

    path = tmp_path / "index.db"
    writer = Store(path)
    chunk = Chunk(session_id="s1", uuid="u1", role="user", text="doppler маскировка",
                  project="p", cwd="/tmp/p", git_branch="main", ts=1785000000,
                  file_path=str(tmp_path / "t.jsonl"), byte_offset=0, byte_len=10,
                  turn_index=0, content_hash="h1", source="claude")
    writer.add(chunk, [0.01] * EMBED_DIM)
    writer.commit()
    writer.close()

    reader = Store(path, read_only=True)
    assert reader.knn([0.01] * EMBED_DIM, 3)          # vector search works
    assert reader.fts("doppler", 3)                   # keyword search works
    assert reader.get_chunk(1).text == "doppler маскировка"
    with pytest.raises(Exception):                    # and writes are refused
        reader.add(chunk, [0.02] * EMBED_DIM)
        reader.commit()
    reader.db.close()
