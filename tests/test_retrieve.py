# tests/test_retrieve.py
import shutil
from pathlib import Path
from session_recall.store import Store
from session_recall.embed import FakeEmbedder
from session_recall.rerank import FakeReranker
from session_recall.index import index_corpus
from session_recall.retrieve import Recall

def _built(tmp_path):
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    shutil.copy("tests/fixtures/session_a.jsonl", proj / "session_a.jsonl")
    store = Store(tmp_path / "r.db")
    emb = FakeEmbedder()
    index_corpus(store, emb, tmp_path / "projects")
    return Recall(store, emb, FakeReranker())

def test_recall_search_finds_relevant(tmp_path):
    r = _built(tmp_path)
    hits = r.recall_search("cache embeddings", k=2)
    assert hits
    assert "cache" in hits[0].snippet.lower()
    assert hits[0].session_id == "sa"

def test_expand_around_returns_raw_turn(tmp_path):
    r = _built(tmp_path)
    hits = r.recall_search("cache embeddings", k=1)
    turns = r.expand_around(hits[0].session_id, hits[0].uuid, before=1, after=1)
    # the assistant turn under the hood includes the thinking block excluded from the index
    assert any("secret reasoning" in t.content or t.type == "thinking" for t in turns)

def test_grep_scans_raw(tmp_path):
    r = _built(tmp_path)
    hits = r.grep("tool output not human")  # only existed under the hood
    assert hits and hits[0].session_id == "sa"

def test_expand_around_multifile_session_resolves_by_uuid(tmp_path):
    """Regression for I1: _file_for must look up by uuid, not session_id.

    session_a.jsonl and session_sidechain.jsonl both carry sessionId="sa".
    Before the fix, _file_for("sa") would return whichever file was found
    first in the DB — meaning side1 (which only exists in sidechain.jsonl)
    could not be expanded because the wrong file was opened.
    """
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    shutil.copy("tests/fixtures/session_a.jsonl", proj / "session_a.jsonl")
    shutil.copy("tests/fixtures/session_sidechain.jsonl", proj / "session_sidechain.jsonl")
    store = Store(tmp_path / "r.db")
    emb = FakeEmbedder()
    index_corpus(store, emb, tmp_path / "projects")
    r = Recall(store, emb, FakeReranker())

    turns = r.expand_around("sa", "side1")
    assert turns, "expand_around returned nothing — uuid lookup broken"
    assert any("sidechain" in t.content for t in turns)


def test_expand_around_output_is_clean_no_signature_or_envelope(tmp_path):
    """expand_around must return readable text — never the base64 thinking
    signature or the full message envelope (the live bug that flooded the agent
    with kilobytes of useless base64/metadata)."""
    import dataclasses
    proj = tmp_path / "projects" / "-Users-me-proj"
    proj.mkdir(parents=True)
    sig = "ErwTCmMIDhgCKkBn13Tkjy" * 40  # base64-like encrypted thinking signature
    (proj / "s.jsonl").write_text("\n".join([
        '{"type":"user","uuid":"u1","sessionId":"sx","message":{"role":"user","content":"the question"}}',
        '{"type":"assistant","uuid":"a1","sessionId":"sx","message":{"role":"assistant","content":'
        '[{"type":"thinking","thinking":"","signature":"' + sig + '"},'
        '{"type":"text","text":"the real answer about caching"}]}}',
    ]) + "\n")
    store = Store(tmp_path / "e.db")
    emb = FakeEmbedder()
    index_corpus(store, emb, tmp_path / "projects")
    r = Recall(store, emb, FakeReranker())
    turns = r.expand_around("sx", "a1", before=1, after=0)
    asst = [t for t in turns if t.type == "assistant"]
    assert asst and "the real answer about caching" in asst[0].content
    blob = str([dataclasses.asdict(t) for t in turns])
    assert sig not in blob, "encrypted thinking signature leaked into expand_around output"
    assert "requestId" not in blob and "cache_read_input_tokens" not in blob, "raw envelope leaked"


def test_recall_search_without_reranker_uses_knn_order(tmp_path):
    """reranker=None must work (graceful fallback): KNN-similarity order, deduped,
    monotonic scores — recall never hard-fails without a reranker (pluggable providers)."""
    import hashlib
    from session_recall.models import Chunk

    def mk(uuid, text, slot):
        v = [0.0] * 1024
        v[slot] = 1.0
        c = Chunk(session_id="s", uuid=uuid, role="assistant", text=text, project="p",
                  cwd="/c", git_branch="b", ts=1, file_path="/f.jsonl", byte_offset=0,
                  byte_len=5, turn_index=0, content_hash=hashlib.sha256(text.encode()).hexdigest())
        return c, v

    store = Store(tmp_path / "nr.db")
    store.add(*mk("u1", "alpha apple", 0))
    store.add(*mk("u2", "beta banana", 1))
    qvec = [0.0] * 1024
    qvec[0] = 1.0  # closest to u1

    class _QEmb(FakeEmbedder):
        def embed_query(self, text):
            return qvec

    hits = Recall(store, _QEmb(), None).recall_search("anything", k=10)  # reranker=None
    assert hits, "no-reranker recall returned nothing"
    assert hits[0].snippet.startswith("alpha apple")   # nearest by KNN distance
    assert hits[0].score >= hits[-1].score             # monotonic
    assert all(isinstance(h.score, float) for h in hits)
    store.close()


def _scoped_chunk(uuid, text, cwd, slot, file_path="/f.jsonl", session_id="s"):
    import hashlib
    from session_recall.models import Chunk
    v = [0.0] * 1024
    v[slot] = 1.0
    c = Chunk(session_id=session_id, uuid=uuid, role="assistant", text=text, project="p",
              cwd=cwd, git_branch="b", ts=1, file_path=file_path, byte_offset=0, byte_len=5,
              turn_index=0, content_hash=hashlib.sha256((uuid + text).encode()).hexdigest())
    return c, v


def test_recall_search_scoped_excludes_other_repos(tmp_path):
    """scope_cwd restricts results to the repo, even when a nearer chunk lives
    in another project."""
    store = Store(tmp_path / "sc.db")
    store.add(*_scoped_chunk("u1", "alpha apple", "/Users/me/repoA", 0))
    store.add(*_scoped_chunk("u2", "alpha apple too", "/Users/me/repoB", 1))
    qvec = [0.0] * 1024
    qvec[1] = 1.0  # nearest is u2 (repoB)

    class _QEmb(FakeEmbedder):
        def embed_query(self, text):
            return qvec

    r = Recall(store, _QEmb(), None)
    glob = r.recall_search("anything", k=10)
    assert any(h.uuid == "u2" for h in glob), "global search should see repoB"
    scoped = r.recall_search("anything", k=10, scope_cwd="/Users/me/repoA")
    assert scoped and all(h.uuid == "u1" for h in scoped), "scoped search leaked another repo"
    store.close()


def test_recall_search_scope_normalizes_worktree(tmp_path):
    """A query cwd inside a worktree must match chunks recorded in the main
    checkout (worktrees collapse to the repo root)."""
    store = Store(tmp_path / "wt.db")
    store.add(*_scoped_chunk("u1", "alpha apple", "/Users/me/repoA", 0))
    qvec = [0.0] * 1024
    qvec[0] = 1.0

    class _QEmb(FakeEmbedder):
        def embed_query(self, text):
            return qvec

    r = Recall(store, _QEmb(), None)
    scoped = r.recall_search("anything", k=10,
                             scope_cwd="/Users/me/repoA/.claude/worktrees/wt-9")
    assert scoped and scoped[0].uuid == "u1", "worktree cwd did not normalize to repo root"
    store.close()


def test_grep_scoped_to_repo(tmp_path):
    fa = tmp_path / "a.jsonl"
    fa.write_text('{"type":"user","uuid":"u1","sessionId":"sa",'
                  '"message":{"role":"user","content":"needle here"}}\n')
    fb = tmp_path / "b.jsonl"
    fb.write_text('{"type":"user","uuid":"u2","sessionId":"sb",'
                  '"message":{"role":"user","content":"needle here"}}\n')
    store = Store(tmp_path / "g.db")
    store.add(*_scoped_chunk("u1", "needle here", "/Users/me/repoA", 0,
                             file_path=str(fa), session_id="sa"))
    store.add(*_scoped_chunk("u2", "needle here", "/Users/me/repoB", 1,
                             file_path=str(fb), session_id="sb"))
    r = Recall(store, FakeEmbedder(), FakeReranker())
    assert r.grep("needle")  # global sees both
    hits = r.grep("needle", scope_cwd="/Users/me/repoA")
    assert hits and all(h.session_id == "sa" for h in hits), "grep scope leaked another repo"
    store.close()


def test_recent_sessions_orders_scopes_and_labels(tmp_path):
    """recent_sessions surfaces the freshest sessions first (the 'what's current /
    how fresh' need from feedback), scoped to the repo, each labelled by its first
    user prompt. The top entry's last_activity is the effective index freshness."""
    import hashlib
    from session_recall.models import Chunk

    def mk(sid, uuid, text, cwd, ts, role, turn):
        return Chunk(session_id=sid, uuid=uuid, role=role, text=text, project="p",
                     cwd=cwd, git_branch="b", ts=ts, file_path="/f.jsonl", byte_offset=0,
                     byte_len=5, turn_index=turn,
                     content_hash=hashlib.sha256((uuid + text).encode()).hexdigest())

    store = Store(tmp_path / "rs.db")
    emb = FakeEmbedder()
    v = emb.embed_documents(["x"])[0]
    store.add(mk("sA", "a1", "question alpha", "/Users/me/repoA", 100, "user", 0), v)
    store.add(mk("sA", "a2", "answer alpha", "/Users/me/repoA", 150, "assistant", 1), v)
    store.add(mk("sB", "b1", "question beta", "/Users/me/repoA", 300, "user", 0), v)
    store.add(mk("sC", "c1", "question gamma", "/Users/me/repoB", 500, "user", 0), v)
    r = Recall(store, emb, None)

    glob = r.recent_sessions(limit=10, now=1000)
    assert [s["session_id"] for s in glob] == ["sC", "sB", "sA"]  # newest first
    assert glob[0]["last_activity"] == 500
    assert "ago" in glob[0]["last_activity_human"]

    scoped = r.recent_sessions(scope_cwd="/Users/me/repoA", limit=10, now=1000)
    assert [s["session_id"] for s in scoped] == ["sB", "sA"]  # repoB excluded
    sA = next(s for s in scoped if s["session_id"] == "sA")
    assert sA["label"].startswith("question alpha")  # first USER prompt, not the answer
    assert sA["turns"] == 2
    store.close()


def test_grep_skips_missing_files_global(tmp_path):
    """Regression: a transcript indexed earlier but later DELETED from disk must
    not crash a global grep. grep iterates every indexed file_path; one missing
    file (FileNotFoundError on open) previously aborted the whole scan. The dead
    file must be skipped and live files still scanned. (Real-world trigger: a
    session under ~/.claude/projects was cleaned up after indexing — its chunks
    linger in the DB pointing at a path that no longer exists.)"""
    live = tmp_path / "live.jsonl"
    live.write_text('{"type":"user","uuid":"u1","sessionId":"sa",'
                    '"message":{"role":"user","content":"needle here"}}\n')
    gone = tmp_path / "gone.jsonl"  # indexed once, never created on disk now
    store = Store(tmp_path / "g.db")
    store.add(*_scoped_chunk("u1", "needle here", "/Users/me/repoA", 0,
                             file_path=str(live), session_id="sa"))
    store.add(*_scoped_chunk("u2", "needle here", "/Users/me/repoB", 1,
                             file_path=str(gone), session_id="sb"))
    r = Recall(store, FakeEmbedder(), FakeReranker())
    hits = r.grep("needle")  # global: touches BOTH the live file and the deleted one
    assert hits and all(h.session_id == "sa" for h in hits), \
        "grep should return live hits and skip the deleted transcript, not crash"
    store.close()


def test_recall_collapses_identical_content_across_sessions(tmp_path):
    """Two chunks with identical text (same content_hash) from different sessions
    must collapse to ONE result — identical text never wastes two top-k slots.
    All rows stay in the DB (provenance preserved)."""
    import hashlib
    from session_recall.models import Chunk

    def mk(sid, uuid, text, fp):
        return Chunk(session_id=sid, uuid=uuid, role="assistant", text=text, project="p",
                     cwd="/c", git_branch="b", ts=1, file_path=fp, byte_offset=0, byte_len=5,
                     turn_index=0, content_hash=hashlib.sha256(text.encode()).hexdigest())

    store = Store(tmp_path / "d.db")
    emb = FakeEmbedder()
    dup = "resilient drop delivery design notes"
    vecs = emb.embed_documents([dup, dup, "completely unrelated subject matter"])
    store.add(mk("s1", "u1", dup, "/a.jsonl"), vecs[0])
    store.add(mk("s2", "u2", dup, "/b.jsonl"), vecs[1])  # identical content, different session
    store.add(mk("s3", "u3", "completely unrelated subject matter", "/c.jsonl"), vecs[2])
    r = Recall(store, emb, FakeReranker())
    hits = r.recall_search(dup, k=10)
    dup_hits = [a for a in hits if a.snippet.startswith("resilient drop")]
    assert len(dup_hits) == 1, f"identical content not collapsed: {len(dup_hits)} copies in top-k"
    # both rows still in the DB (provenance preserved, not deleted)
    assert store.db.execute(
        "SELECT count(*) FROM chunks WHERE content_hash = ?",
        (hashlib.sha256(dup.encode()).hexdigest(),)).fetchone()[0] == 2
    store.close()
