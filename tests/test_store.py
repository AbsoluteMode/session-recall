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
