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
    out = server.recall_search("cache embeddings", k=1)
    assert out and "cache" in out[0]["snippet"]


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
    out = server.recall_search("anything", k=10, scope_cwd="/Users/me/repoA")
    assert out and all(o["uuid"] == "u1" for o in out), "server did not forward scope_cwd to recall"
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
    out = server.grep("needle", scope_cwd="/Users/me/repoA")
    assert out and all(o["session_id"] == "sa" for o in out), "server did not forward scope_cwd to grep"
    store.close()
