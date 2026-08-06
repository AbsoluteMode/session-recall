# tests/test_server.py
import shutil
from session_recall.store import Store
from session_recall.embed import FakeEmbedder
from session_recall.rerank import FakeReranker
from session_recall.index import index_corpus
from session_recall.retrieve import Recall
import session_recall.server as server

def test_tool_functions_delegate(tmp_path, monkeypatch):
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    shutil.copy("tests/fixtures/session_a.jsonl", proj / "session_a.jsonl")
    store = Store(tmp_path / "s.db")
    index_corpus(store, FakeEmbedder(), tmp_path / "projects")
    monkeypatch.setattr(server, "_recall", Recall(store, FakeEmbedder(), FakeReranker()))
    out = server.recall_search("cache embeddings", k=1)["anchors"]
    assert out and "cache" in out[0]["snippet"]

    same_day = server.recall_search(
        "cache embeddings", k=1, on_date="2026-06-01", timezone="UTC")["anchors"]
    assert same_day and same_day[0]["session_id"] == "sa"
    assert server.recall_search(
        "cache embeddings", k=1, start_date="2026-06-02",
        end_date="2026-06-02", timezone="UTC")["anchors"] == []


def _mk(uuid, text, cwd, slot, file_path="/f.jsonl", session_id="s"):
    import hashlib
    from session_recall.models import Chunk
    v = [0.0] * 1024
    v[slot] = 1.0
    c = Chunk(session_id=session_id, uuid=uuid, role="assistant", text=text, project="p",
              cwd=cwd, git_branch="b", ts=1, file_path=file_path, byte_offset=0, byte_len=5,
              turn_index=0, content_hash=hashlib.sha256((uuid + text).encode()).hexdigest())
    return c, v


def test_recall_search_forwards_scope_cwd(tmp_path, monkeypatch):
    store = Store(tmp_path / "sc.db")
    store.add(*_mk("u1", "alpha", "/Users/me/repoA", 0))
    store.add(*_mk("u2", "alpha too", "/Users/me/repoB", 1))
    qvec = [0.0] * 1024
    qvec[1] = 1.0  # nearest is u2 (repoB)

    class _QEmb(FakeEmbedder):
        def embed_query(self, text):
            return qvec

    monkeypatch.setattr(server, "_recall", Recall(store, _QEmb(), None))
    out = server.recall_search("anything", k=10, scope_cwd="/Users/me/repoA")["anchors"]
    assert out and all(o["uuid"] == "u1" for o in out), "server did not forward scope_cwd to recall"
    store.close()


def test_recall_search_enriches_with_human_timestamp(tmp_path, monkeypatch):
    store = Store(tmp_path / "h.db")
    store.add(*_mk("u1", "alpha", "/Users/me/repoA", 0))
    monkeypatch.setattr(server, "_recall", Recall(store, FakeEmbedder(), FakeReranker()))
    out = server.recall_search("alpha", k=1)["anchors"]
    assert out and "when_human" in out[0], "raw epoch not enriched with when_human"
    store.close()


def test_solo_anchors_carry_no_owner_field(tmp_path, monkeypatch):
    """A one-person index has nobody to attribute to, so the field is dropped.

    Kept out of the payload rather than sent as null: an agent reading
    "owner": null on every anchor learns nothing, and the field stays a real
    signal on a team hub, where it is filled.
    """
    store = Store(tmp_path / "solo.db")
    store.add(*_mk("u1", "alpha", "/Users/me/repoA", 0))
    monkeypatch.setattr(server, "_recall", Recall(store, FakeEmbedder(), FakeReranker()))
    out = server.recall_search("alpha", k=1)["anchors"]
    assert out and "owner" not in out[0]
    store.close()


def test_recall_search_reports_fts_only_degrade(tmp_path, monkeypatch):
    """Embedder unreachable -> search runs on FTS alone. The response MUST say so:
    without the flag a lexical result set is indistinguishable from a semantic one,
    and the agent concludes the history is empty when it is merely unsearchable."""
    store = Store(tmp_path / "deg.db")
    store.add(*_mk("u1", "alpha needle", "/Users/me/repoA", 0))

    class _DownEmb(FakeEmbedder):
        def embed_query(self, text):
            raise RuntimeError("embedding api down")

    monkeypatch.setattr(server, "_recall", Recall(store, _DownEmb(), None))
    out = server.recall_search("needle", k=1)
    assert out["anchors"], "FTS-only degrade must still return keyword hits"
    assert out["degraded"], "degrade to FTS-only is not reported in the response"
    store.close()


def test_recall_search_healthy_is_not_flagged(tmp_path, monkeypatch):
    """A working embedder must leave the flag clear — otherwise the warning is noise
    and gets ignored exactly when it matters."""
    store = Store(tmp_path / "ok.db")
    store.add(*_mk("u1", "alpha needle", "/Users/me/repoA", 0))
    monkeypatch.setattr(server, "_recall", Recall(store, FakeEmbedder(), FakeReranker()))
    out = server.recall_search("needle", k=1)
    assert out["degraded"] is None, "healthy search must not be flagged as degraded"
    store.close()


def test_recent_sessions_tool_delegates_and_scopes(tmp_path, monkeypatch):
    store = Store(tmp_path / "rs.db")
    store.add(*_mk("u1", "hello", "/Users/me/repoA", 0, session_id="s1"))
    store.add(*_mk("u2", "hello", "/Users/me/repoB", 1, session_id="s2"))
    monkeypatch.setattr(server, "_recall", Recall(store, FakeEmbedder(), FakeReranker()))
    out = server.recent_sessions(scope_cwd="/Users/me/repoA")
    assert [s["session_id"] for s in out] == ["s1"], "recent_sessions did not scope to repo"
    assert "last_activity_human" in out[0]
    store.close()


def test_grep_forwards_scope_cwd(tmp_path, monkeypatch):
    fa = tmp_path / "a.jsonl"
    fa.write_text('{"type":"user","uuid":"u1","sessionId":"sa",'
                  '"message":{"role":"user","content":"needle"}}\n')
    fb = tmp_path / "b.jsonl"
    fb.write_text('{"type":"user","uuid":"u2","sessionId":"sb",'
                  '"message":{"role":"user","content":"needle"}}\n')
    store = Store(tmp_path / "g.db")
    store.add(*_mk("u1", "needle", "/Users/me/repoA", 0, file_path=str(fa), session_id="sa"))
    store.add(*_mk("u2", "needle", "/Users/me/repoB", 1, file_path=str(fb), session_id="sb"))
    monkeypatch.setattr(server, "_recall", Recall(store, FakeEmbedder(), FakeReranker()))
    out = server.grep("needle", scope_cwd="/Users/me/repoA", limit=1)
    assert out and all(o["session_id"] == "sa" for o in out), "server did not forward scope_cwd to grep"
    assert len(out) == 1
    store.close()
