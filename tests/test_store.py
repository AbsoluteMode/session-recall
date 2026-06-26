from session_recall.store import Store
from session_recall.models import Chunk

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

def test_indexed_marker(tmp_path):
    s = Store(tmp_path / "t.db")
    assert not s.is_indexed("/f.jsonl", "sig1")
    s.mark_indexed("/f.jsonl", "sig1")
    assert s.is_indexed("/f.jsonl", "sig1")
    assert not s.is_indexed("/f.jsonl", "sig2")  # changed signature
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
