"""What ranks and what does not: summaries, heartbeats, long turns, recency.

Shapes taken from a real index on 2026-11-08: 9 169 of 40 035 chunks were one
Codex automation's <heartbeat> prompt and reply; 728 chunks were compacted-
session summaries averaging 23 KB that outranked the turns they summarized;
"the latest PR" returned a July PR above the October one; 931 chunks were
longer than the embedder's token budget.
WHY: docs/decisions/2026-11-08-recall-noise-recency-subchunks.md
"""

import hashlib
import json
import math
from pathlib import Path

from session_recall import config
from session_recall.chunking import split_text, surface_role
from session_recall.embed import FakeEmbedder
from session_recall.extract import extract_file
from session_recall.index import index_corpus
from session_recall.models import Chunk
from session_recall.repos import RepoResolver
from session_recall.rerank import FakeReranker
from session_recall.retrieve import Recall, asks_for_current
from session_recall.store import Store, fts_match
from session_recall.transcripts import extract_codex_file
import session_recall.server as server

T0 = 1792900000  # 2026-10-25
DAY = 86400
HEARTBEAT = ("<heartbeat>\n  <automation_id>w</automation_id>\n"
             "  <current_time_iso>{iso}</current_time_iso>\n"
             "  <instructions>Ты — сборщик очереди Whytap.</instructions>\n</heartbeat>")
SUMMARY = ("This session is being continued from a previous conversation that ran "
           "out of context. The summary below covers the earlier part.\n\nSummary:\n"
           "1. Primary Request: fix the whytap build queue; tokens cache embeddings\n")


def _iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def user(sid, uuid, ts, text, cwd="/home/me/proj"):
    return {"type": "user", "sessionId": sid, "uuid": uuid, "cwd": cwd, "gitBranch": "main",
            "timestamp": _iso(ts), "entrypoint": "claude-desktop",
            "message": {"role": "user", "content": text}}


def assistant(sid, uuid, ts, text, cwd="/home/me/proj"):
    return {"type": "assistant", "sessionId": sid, "uuid": uuid, "cwd": cwd,
            "gitBranch": "main", "timestamp": _iso(ts),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def write_session(projects: Path, name: str, lines: list[dict]) -> Path:
    folder = projects / f"-home-me-{name}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _build(tmp_path, embedder=None):
    store = Store(tmp_path / "q.db")
    emb = embedder or FakeEmbedder()
    index_corpus(store, emb, tmp_path / "projects",
                 resolver=RepoResolver(home=str(tmp_path)))
    return store, Recall(store, emb, FakeReranker())


# --- 1. splitting long turns ------------------------------------------------

def test_split_text_keeps_short_text_whole():
    assert split_text("short", max_chars=100) == ["short"]
    assert split_text("x" * 100, max_chars=100) == ["x" * 100]


def test_split_text_breaks_at_paragraphs_and_overlaps():
    paras = [f"Paragraph {i}: " + ("слово " * 30).strip() for i in range(12)]
    text = "\n\n".join(paras)
    pieces = split_text(text, max_chars=600, overlap=100)
    assert len(pieces) > 1
    assert all(len(p) <= 600 for p in pieces)
    # every paragraph is still findable in full in some piece
    for para in paras:
        assert any(para in p for p in pieces), para[:20]
    # consecutive pieces share text (the overlap), and nothing is lost
    for a, b in zip(pieces, pieces[1:]):
        tail = a[-40:]
        assert any(word in b for word in tail.split()[1:3])


def test_split_text_makes_progress_without_whitespace():
    pieces = split_text("x" * 5000, max_chars=1000, overlap=100)
    assert len(pieces) == 6 and "".join(p[-900:] for p in pieces)  # terminates
    assert pieces[0] == "x" * 1000


def test_long_turn_becomes_several_chunks_sharing_the_uuid(tmp_path):
    long = "\n\n".join(f"Step {i}. " + "detail " * 60 for i in range(10))
    path = write_session(tmp_path / "projects", "long",
                         [user("s", "s-u1", T0, "plan?"), assistant("s", "s-a1", T0 + 1, long)])
    chunks = extract_file(str(path), project="long")
    parts = [c for c in chunks if c.uuid == "s-a1"]
    assert len(parts) > 1 and all(len(c.text) <= 2000 for c in parts)
    assert len({c.content_hash for c in parts}) == len(parts)
    assert all((c.byte_offset, c.byte_len, c.turn_index) == (parts[0].byte_offset,
               parts[0].byte_len, parts[0].turn_index) for c in parts)


def test_search_returns_a_long_turn_once(tmp_path):
    long = "\n\n".join(f"Step {i}. " + "detail " * 60 for i in range(10))
    long += "\n\nFinal step. The refresh token conflict came from the clock skew."
    write_session(tmp_path / "projects", "long",
                  [user("s", "s-u1", T0, "plan?"), assistant("s", "s-a1", T0 + 1, long),
                   assistant("s", "a2", T0 + 2, "Unrelated remark about lunch.")])
    _, recall = _build(tmp_path)
    hits = recall.recall_search("refresh token conflict clock skew", k=10, per_session=0)
    uuids = [h.uuid for h in hits]
    assert uuids.count("s-a1") == 1, uuids
    assert "clock skew" in hits[0].snippet  # the matching PART, not the opening


# --- 2. roles: summary / automation -----------------------------------------

def test_surface_role_classifies_machinery():
    assert surface_role("user", HEARTBEAT.format(iso="t")) == "automation"
    assert surface_role("assistant", "  " + HEARTBEAT.format(iso="t")) == "automation"
    assert surface_role("user", SUMMARY) == "summary"
    assert surface_role("assistant", SUMMARY) == "assistant"  # only a user turn carries one
    assert surface_role("user", "what is <heartbeat>?") == "user"


def test_claude_extraction_marks_summary_and_heartbeat(tmp_path):
    path = write_session(tmp_path / "projects", "roles", [
        user("s", "s-u1", T0, SUMMARY),
        user("s", "u2", T0 + 1, HEARTBEAT.format(iso="a")),
        assistant("s", "a2", T0 + 2, HEARTBEAT.format(iso="a") + "DONT_NOTIFY"),
        user("s", "u3", T0 + 3, "real question\n\n<side-sessions-event kind=\"done\">"
             "session x finished</side-sessions-event>"),
    ])
    chunks = {c.uuid: c for c in extract_file(str(path), project="roles")}
    assert chunks["s-u1"].role == "summary"
    assert chunks["u2"].role == chunks["a2"].role == "automation"
    assert chunks["u3"].role == "user" and chunks["u3"].text == "real question"


def test_codex_extraction_marks_heartbeats(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "cx", "cwd": "/repo"}},
        {"timestamp": "2026-10-19T19:19:19Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": HEARTBEAT.format(iso="a")}},
        {"timestamp": "2026-10-19T19:19:20Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": HEARTBEAT.format(iso="a") + "x"}},
        {"timestamp": "2026-10-19T19:20:00Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "собери релиз"}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    roles = [(c.role, c.text[:12]) for c in extract_codex_file(str(path))]
    assert roles == [("automation", "<heartbeat>\n"), ("automation", "<heartbeat>\n"),
                     ("user", "собери релиз")]


def test_automation_rows_are_stored_without_vectors(tmp_path):
    write_session(tmp_path / "projects", "beat", [
        user("b", "b-u1", T0, HEARTBEAT.format(iso="a")),
        assistant("b", "b-a1", T0 + 1, HEARTBEAT.format(iso="a") + "x"),
    ])
    emb = FakeEmbedder()
    store, recall = _build(tmp_path, emb)
    assert emb.doc_calls == 0, "nothing to embed in a heartbeat-only file"
    assert store.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
    assert store.db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 0
    assert store.fts("automation_id", 5), "still greppable through fts"
    assert recall.recall_search("Whytap очередь сборщик", k=5) == []
    # and the session still exists for the status board
    assert recall.recent_sessions(now=T0 + 5)[0]["session_id"] == "b"


def test_summaries_rank_only_on_request(tmp_path):
    write_session(tmp_path / "projects", "sum", [
        user("s", "s-u1", T0, SUMMARY),
        user("s", "u2", T0 + 1, "how do we cache embeddings?"),
        assistant("s", "a2", T0 + 2, "We cache embeddings in sqlite."),
    ])
    _, recall = _build(tmp_path)
    default = recall.recall_search("cache embeddings", k=10, per_session=0)
    assert [h.role for h in default] and "summary" not in {h.role for h in default}
    with_sum = recall.recall_search("cache embeddings", k=10, per_session=0,
                                    include_summaries=True)
    assert "summary" in {h.role for h in with_sum}


def test_status_board_ignores_heartbeat_replies(tmp_path):
    write_session(tmp_path / "projects", "mix", [
        user("m", "u1", T0, "собери релиз"),
        assistant("m", "a1", T0 + 1, "Релиз собран, тесты зелёные."),
        user("m", "u2", T0 + 2, HEARTBEAT.format(iso="a")),
        assistant("m", "a2", T0 + 3, HEARTBEAT.format(iso="a") + "DONT_NOTIFY"),
    ])
    _, recall = _build(tmp_path)
    entry = recall.recent_sessions(now=T0 + 10)[0]
    assert entry["last_assistant"].startswith("Релиз собран")
    assert entry["last_user"] == "собери релиз"
    assert entry["turns"] == 4  # heartbeats still count as activity


# --- 3. recency --------------------------------------------------------------

def test_asks_for_current_detects_temporal_wording():
    assert asks_for_current("последний PR по session-recall")
    assert asks_for_current("what is the latest decision on auth")
    assert asks_for_current("что сейчас в работе")
    assert not asks_for_current("why did refresh tokens conflict")
    assert not asks_for_current("current_time_iso field")  # an identifier, not a tense


def _two_prs(tmp_path):
    q = "latest PR session recall merged squash green ci"
    write_session(tmp_path / "projects", "old", [
        user("old", "old-u1", T0 - 100 * DAY, "status?"),
        assistant("old", "old-a1", T0 - 100 * DAY + 1,
                  "latest PR session recall merged squash green ci twenty-two"),
    ])
    write_session(tmp_path / "projects", "new", [
        user("new", "new-u1", T0 - 2 * DAY, "status?"),
        assistant("new", "new-a1", T0 - 2 * DAY + 1,
                  "latest PR session recall merged squash green sixty-two today"),
    ])
    return q


def test_recency_breaks_near_ties_when_asked_for_current(tmp_path):
    q = _two_prs(tmp_path)
    _, recall = _build(tmp_path)
    # relevance alone (lexical overlap 8/9 vs 7/9): the July-style hit wins
    plain = recall.recall_search(q, k=5, recent_first=False, now=T0)
    assert [h.session_id for h in plain][:2] == ["old", "new"]
    # the query says "latest": the fresher near-tie wins
    auto = recall.recall_search(q, k=5, now=T0)
    assert [h.session_id for h in auto][:2] == ["new", "old"]
    # scores stay relevance; the order carries the recency
    assert auto[0].score < auto[1].score


def test_recent_first_forces_the_bonus_without_wording(tmp_path):
    _two_prs(tmp_path)
    _, recall = _build(tmp_path)
    q = "PR session recall merged squash green ci"
    assert recall.recall_search(q, k=5, now=T0)[0].session_id == "old"
    assert recall.recall_search(q, k=5, recent_first=True, now=T0)[0].session_id == "new"


def test_recency_never_lifts_an_irrelevant_hit(tmp_path):
    write_session(tmp_path / "projects", "old", [
        user("old", "old-u1", T0 - 100 * DAY, "status?"),
        assistant("old", "old-a1", T0 - 100 * DAY + 1, "latest PR merged squash"),
    ])
    write_session(tmp_path / "projects", "new", [
        user("new", "new-u1", T0 - 1 * DAY, "status?"),
        assistant("new", "new-a1", T0 - 1 * DAY + 1,
                  "latest lunch was pasta and the weather stayed grey all afternoon"),
    ])
    _, recall = _build(tmp_path)
    hits = recall.recall_search("latest PR merged squash", k=5, now=T0)
    assert hits[0].session_id == "old"


# --- 4. stale meta docs --------------------------------------------------------

def _doc(uuid, text, ts):
    return Chunk(session_id=uuid, uuid=uuid, role="doc", text=text, project="p", cwd="",
                 git_branch="", ts=ts, file_path=f"/docs/{uuid}.md", byte_offset=0,
                 byte_len=0, turn_index=0,
                 content_hash=hashlib.sha256(text.encode()).hexdigest(), source="metadocs")


def test_old_meta_docs_are_flagged_stale(tmp_path):
    store = Store(tmp_path / "d.db")
    emb = FakeEmbedder()
    old = _doc("dec-old", "decision: embedder is voyage large", T0 - 95 * DAY)
    new = _doc("dec-new", "decision: embedder is qwen via inference api", T0 - 3 * DAY)
    for c in (old, new):
        store.add(c, emb.embed_documents([c.text])[0])
    store.commit()
    recall = Recall(store, emb, FakeReranker())
    hits = {h.uuid: h for h in recall.recall_search("decision embedder", k=5, now=T0)}
    assert hits["dec-old"].stale is True
    assert hits["dec-new"].stale is None
    out = {d["uuid"]: d for d in (server._adict(h) for h in hits.values())}
    assert out["dec-old"]["stale"] is True and "stale" not in out["dec-new"]


# --- 5. keyword fallback ---------------------------------------------------------

def test_fts_match_drops_stopwords_and_matches_prefixes():
    assert fts_match("почему отключили hub.json и перешли на индекс") == \
        '"поче"* OR "отключи"* OR "hub.js"* OR "переш"* OR "инде"*'
    assert fts_match("и на") == '"и"* OR "на"*'  # nothing longer: keep what there is
    assert fts_match("PR 62") == '"62"*'
    assert fts_match("") == ""


def test_fts_finds_inflected_forms(tmp_path):
    store = Store(tmp_path / "f.db")
    emb = FakeEmbedder()
    c = _doc("x", "Гармония ждёт индексацию", T0)
    c.source, c.role = "claude", "assistant"
    store.add(c, emb.embed_documents([c.text])[0])
    store.commit()
    assert store.fts("Гармонии", 5)
    assert store.fts("индексации", 5)
    assert not store.fts("Воронеж", 5)


# --- 6. review findings (Codex, 2026-11-08) --------------------------------------

def test_extractor_bump_reuses_vectors_of_unchanged_text(tmp_path, monkeypatch):
    """A new extractor version re-extracts every file; it must not re-embed
    text whose hash is unchanged — an upgrade would otherwise resend the
    corpus. Vectors depend on the text and the embedding space only."""
    import session_recall.extract as extract
    write_session(tmp_path / "projects", "keep", [
        user("k", "u1", T0, "how do we cache embeddings?"),
        assistant("k", "a1", T0 + 1, "We cache embeddings in sqlite."),
    ])
    emb = FakeEmbedder()
    store, _ = _build(tmp_path, emb)
    calls = emb.doc_calls
    monkeypatch.setattr(extract, "extractor_version", lambda source: "99")
    added = index_corpus(store, emb, tmp_path / "projects",
                         resolver=RepoResolver(home=str(tmp_path)))
    assert added == 2, "the file was re-extracted under the new version"
    assert emb.doc_calls == calls, "unchanged text must reuse its vectors"
    assert store.db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 2


def test_multi_block_heartbeat_reply_is_one_automation_turn(tmp_path):
    path = write_session(tmp_path / "projects", "blocks", [
        user("b", "b-u1", T0, "status?"),
        {"type": "assistant", "sessionId": "b", "uuid": "a1", "cwd": "/home/me/proj",
         "gitBranch": "main", "timestamp": _iso(T0 + 1),
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": HEARTBEAT.format(iso="a")},
             {"type": "text", "text": "DONT_NOTIFY"}]}},
    ])
    roles = {(c.uuid, c.text[:11]): c.role for c in extract_file(str(path), project="blocks")}
    assert roles[("a1", "DONT_NOTIFY")] == "automation"
    assert roles[("b-u1", "status?")] == "user"


def test_one_long_turn_cannot_crowd_out_another_match(tmp_path):
    """150 matching pieces of one answer must not fill the candidate pool
    before a second, shorter matching turn gets in."""
    long = "\n\n".join(f"Note {i}: cache failure seen again in run {i} " + "filler " * 40
                       for i in range(150))
    write_session(tmp_path / "projects", "long", [
        user("l", "l-u1", T0, "why?"), assistant("l", "l-a1", T0 + 1, long)])
    write_session(tmp_path / "projects", "short", [
        user("s", "s-u1", T0 + 10, "why?"),
        assistant("s", "s-a1", T0 + 11, "The cache failure came from the tokenizer.")])
    _, recall = _build(tmp_path)
    hits = recall.recall_search("cache failure", k=10, per_session=0)
    assert {h.session_id for h in hits} == {"l", "s"}


def test_fts_only_search_keeps_lexical_order_under_recent_first(tmp_path):
    """Degraded (no embedder, no reranker): every hit has score None, so the
    recency bonus has nothing to ride on — bm25 order must stand."""
    write_session(tmp_path / "projects", "old", [
        user("old", "old-u1", T0 - 100 * DAY, "?"),
        assistant("old", "old-a1", T0 - 100 * DAY + 1,
                  "cache cache cache tokens refresh failure")])
    write_session(tmp_path / "projects", "new", [
        user("new", "new-u1", T0 - 1 * DAY, "?"),
        assistant("new", "new-a1", T0 - 1 * DAY + 1, "latest pasta dish for lunch")])

    class Broken(FakeEmbedder):
        def embed_query(self, text):
            raise RuntimeError("embedder down")

    store, _ = _build(tmp_path)
    recall = Recall(store, Broken(), None)
    hits = recall.recall_search("latest cache tokens refresh failure", k=5,
                                recent_first=True, now=T0)
    assert hits.degraded and hits[0].session_id == "old"
    assert all(h.score is None for h in hits)


def test_duplicate_text_surfaces_as_its_newest_occurrence_when_asked(tmp_path):
    text = "Release 62 merged successfully."
    write_session(tmp_path / "projects", "older", [
        user("older", "older-u1", T0 - 100 * DAY, "?"),
        assistant("older", "older-a1", T0 - 100 * DAY + 1, text)])
    write_session(tmp_path / "projects", "newer", [
        user("newer", "newer-u1", T0 - 1 * DAY, "?"),
        assistant("newer", "newer-a1", T0 - 1 * DAY + 1, text)])
    _, recall = _build(tmp_path)
    hits = recall.recall_search("Release 62 merged", k=5, recent_first=True, now=T0)
    merged = [h for h in hits if h.snippet == text]
    assert len(merged) == 1 and merged[0].session_id == "newer"


def test_fts_match_ignores_punctuation_only_terms():
    assert fts_match("CI ???") == '"CI"*'
    assert fts_match('"hello", world!') == '"hell"* OR "worl"*'
    assert fts_match("???") == ""


def test_session_turn_count_ignores_sub_chunks(tmp_path):
    long = " ".join(f"token{i}" for i in range(600))
    write_session(tmp_path / "projects", "cnt", [
        user("c", "u1", T0, "go"), assistant("c", "a1", T0 + 1, long)])
    store, recall = _build(tmp_path)
    assert store.db.execute("SELECT count(*) FROM chunks").fetchone()[0] > 2
    assert recall.recent_sessions(now=T0 + 5)[0]["turns"] == 2


def test_metadocs_collector_keeps_a_split_turn_together(tmp_path):
    from session_recall.metadocs import collect
    from session_recall.metadocs.config import Watermarks
    long = "\n\n".join(f"Paragraph {i}. " + "слово " * 80 for i in range(6))
    write_session(tmp_path / "projects", "md", [
        user("m", "u1", T0, "расскажи"), assistant("m", "a1", T0 + 1, long)])
    store, _ = _build(tmp_path)
    assert store.db.execute("SELECT count(*) FROM chunks WHERE uuid='a1'").fetchone()[0] > 1
    project = store.db.execute("SELECT project FROM chunks LIMIT 1").fetchone()[0]
    (upd,) = collect.pending_sessions(store.db, project, Watermarks(tmp_path / "marks.json"))
    assert [t["role"] for t in upd.turns] == ["user", "assistant"]
    assert "Paragraph 0." in upd.turns[1]["text"] and "Paragraph 5." in upd.turns[1]["text"]


# --- 7. review findings, second pass ------------------------------------------------

def test_keyword_exact_match_survives_a_full_semantic_pool(tmp_path):
    """FTS is the exact-identifier path. 300 semantically-near turns must not
    push the one row containing the error string out of the candidates."""
    lines = [user("many", "u0", T0, "logs?")]
    lines += [assistant("many", f"a{i}", T0 + i, f"Routine deploy note number {i}, all green.")
              for i in range(1, 320)]
    lines.append(assistant("many", "needle", T0 + 400,
                           "Build failed: NEEDLE_EXACT_ERROR in the tokenizer step."))
    write_session(tmp_path / "projects", "many", lines)
    _, recall = _build(tmp_path)
    hits = recall.recall_search("NEEDLE_EXACT_ERROR", k=5, per_session=0)
    assert hits and hits[0].uuid == "needle"


def test_three_hundred_pieces_still_leave_room_for_another_turn(tmp_path):
    long = "\n\n".join(f"cache failure {i} " + "detail " * 275 for i in range(300))
    write_session(tmp_path / "projects", "long", [
        user("l", "l-u1", T0, "why?"), assistant("l", "l-a1", T0 + 1, long)])
    write_session(tmp_path / "projects", "short", [
        user("s", "s-u1", T0 + 10, "why?"),
        assistant("s", "s-a1", T0 + 11, "The cache failure came from the tokenizer.")])
    _, recall = _build(tmp_path)
    hits = recall.recall_search("cache failure", k=10, per_session=0)
    assert {h.session_id for h in hits} == {"l", "s"}


def test_large_candidates_argument_stays_within_sqlite_vec_limit(tmp_path):
    write_session(tmp_path / "projects", "one", [
        user("o", "u1", T0, "needle?"), assistant("o", "a1", T0 + 1, "a needle in sqlite")])
    _, recall = _build(tmp_path)
    hits = recall.recall_search("needle", k=5, candidates=2000)
    assert hits.degraded is None and hits


def test_duplicate_replacement_keeps_other_pieces_of_the_old_turn(tmp_path):
    shared = "Release 62 merged successfully."
    write_session(tmp_path / "projects", "older", [
        user("older", "older-u1", T0 - 100 * DAY, "?"),
        assistant("older", "older-a1", T0 - 100 * DAY + 1, shared),
        assistant("older", "a2", T0 - 100 * DAY + 2, "Release 62 notes: NEEDLE_EXACT_ERROR fixed."),
    ])
    write_session(tmp_path / "projects", "newer", [
        user("newer", "newer-u1", T0 - DAY, "?"), assistant("newer", "newer-a1", T0 - DAY + 1, shared)])
    _, recall = _build(tmp_path)
    hits = recall.recall_search("Release 62 NEEDLE_EXACT_ERROR", k=10, per_session=0,
                                recent_first=True, now=T0)
    uuids = {(h.session_id, h.uuid) for h in hits}
    assert ("older", "a2") in uuids and ("newer", "newer-a1") in uuids
    assert ("older", "older-a1") not in uuids


def test_sig_embed_fp_tolerates_colons_in_model_names():
    from session_recall.index import _sig_embed_fp
    assert _sig_embed_fp("v3:openai-compatible/nomic-embed-text:latest/1024:123:456") == \
        "openai-compatible/nomic-embed-text:latest/1024"
    assert _sig_embed_fp("codex-v3:inference-api/embedder/4096/abcd:77:123:456") == \
        "inference-api/embedder/4096/abcd"
    assert _sig_embed_fp("v2:123:456") == ""


def test_join_pieces_removes_the_shared_overlap():
    from session_recall.chunking import join_pieces
    text = " ".join(f"w{i:04d}" for i in range(650)) + " FINAL_VERDICT"
    pieces = split_text(text)
    assert len(pieces) > 1
    whole = pieces[0]
    for piece in pieces[1:]:
        whole = join_pieces(whole, piece)
    assert whole == text
    assert join_pieces("abc", "xyz") == "abc\nxyz"


def test_metadocs_collector_does_not_duplicate_overlap(tmp_path):
    from session_recall.metadocs import collect
    from session_recall.metadocs.config import Watermarks
    text = " ".join(f"w{i:04d}" for i in range(650)) + " FINAL_VERDICT"
    write_session(tmp_path / "projects", "md2", [
        user("m", "u1", T0, "итог?"), assistant("m", "a1", T0 + 1, text)])
    store, _ = _build(tmp_path)
    project = store.db.execute("SELECT project FROM chunks LIMIT 1").fetchone()[0]
    (upd,) = collect.pending_sessions(store.db, project, Watermarks(tmp_path / "marks.json"))
    merged = upd.turns[1]["text"]
    assert merged.endswith("FINAL_VERDICT") and merged.count("w0100") == 1
    assert "_full" not in upd.turns[1]


# --- 8. review findings, third pass --------------------------------------------------

def test_keyword_hit_cut_by_the_semantic_cap_still_reaches_the_pool(tmp_path):
    """250 fillers sit nearer to the query vector than the one turn holding
    the exact error string. The KNN pool keeps 200 of them and fetched the
    needle too; the FTS path must still put the needle in the candidates."""
    import session_recall.config as config
    dim = config.EMBED_DIM
    store = Store(tmp_path / "cap.db")
    near = [0.0] * dim
    near[0] = 1.0
    far = [0.0] * dim
    far[1] = 1.0
    for i in range(250):
        c = _doc(f"f{i}", f"Routine deploy note number {i}, all green.", T0 + i)
        c.source, c.role, c.session_id = "claude", "assistant", "many"
        store.add(c, near)
    needle = _doc("needle", "Build failed: NEEDLE_EXACT_ERROR in the tokenizer step.", T0 + 400)
    needle.source, needle.role, needle.session_id = "claude", "assistant", "many"
    store.add(needle, far)
    store.commit()

    class NearQuery(FakeEmbedder):
        def embed_query(self, text):
            return near

    recall = Recall(store, NearQuery(), FakeReranker())
    hits = recall.recall_search("NEEDLE_EXACT_ERROR", k=5, per_session=0)
    assert hits and hits[0].uuid == "needle"


def test_join_pieces_does_not_merge_a_coincidental_short_match():
    from session_recall.chunking import join_pieces
    assert join_pieces("We use redis", "disk cache is disabled") == \
        "We use redis\ndisk cache is disabled"


def test_metadocs_collector_keeps_separate_text_blocks_apart(tmp_path):
    from session_recall.metadocs import collect
    from session_recall.metadocs.config import Watermarks
    write_session(tmp_path / "projects", "blocks2", [
        user("b", "b-u1", T0, "cache?"),
        {"type": "assistant", "sessionId": "b", "uuid": "a1", "cwd": "/home/me/proj",
         "gitBranch": "main", "timestamp": _iso(T0 + 1),
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "We use redis"},
             {"type": "text", "text": "disk cache is disabled"}]}},
    ])
    store, _ = _build(tmp_path)
    project = store.db.execute("SELECT project FROM chunks LIMIT 1").fetchone()[0]
    (upd,) = collect.pending_sessions(store.db, project, Watermarks(tmp_path / "marks.json"))
    assert upd.turns[1]["text"] == "We use redis\ndisk cache is disabled"


# --- 9. resumed-session copies ------------------------------------------------------

def _resumed_copies(tmp_path):
    """One two-piece turn (uuid t1) present in the original session `orig` and
    in its resume copy `res` — identical text, so identical hashes — whose
    vectors were embedded at different times and drifted a little, as a GPU
    embedder's do. By distance the pieces interleave: A(res), B(orig),
    A(orig), B(res). Hash dedup then keeps A from one copy and B from the
    other. Returns the Recall; the vectors are built around e0."""
    store = Store(tmp_path / "copies.db")
    dim = config.EMBED_DIM

    def vec(eps):
        v = [0.0] * dim
        v[0], v[1] = 1.0, eps
        norm = math.sqrt(1.0 + eps * eps)
        return [x / norm for x in v]

    class PinnedQuery(FakeEmbedder):
        def embed_query(self, text):
            return vec(0.0)

    text_a = "Part A: the Codex bridge is an MCP link between the two agents."
    text_b = "Part B: the Codex bridge relays questions over MCP both ways."
    pieces = [("res", text_a, 0.1), ("orig", text_b, 0.2),
              ("orig", text_a, 0.3), ("res", text_b, 0.4)]
    for sid, text, eps in pieces:
        store.add(Chunk(session_id=sid, uuid="t1", role="assistant", text=text,
                        project="proj", cwd="/home/me/proj", git_branch="main",
                        ts=T0, file_path=f"/t/{sid}.jsonl", byte_offset=0,
                        byte_len=1, turn_index=1,
                        content_hash=hashlib.sha256(text.encode()).hexdigest()),
                  vec(eps))
    store.commit()
    return Recall(store, PinnedQuery(dim=dim), FakeReranker())


def test_a_turn_copied_by_a_resumed_session_is_one_hit(tmp_path):
    """249 Claude turns in the 2026-11-08 index live under two session_ids:
    the original and the resume-created copy. When hash dedup keeps piece A
    from one copy and piece B from the other, the per-turn collapse must still
    see one turn — the uuid, not the session, is the turn's identity."""
    recall = _resumed_copies(tmp_path)
    hits = recall.recall_search("Codex bridge MCP", k=10, per_session=0)
    assert [h.uuid for h in hits] == ["t1"], [(h.session_id, h.uuid) for h in hits]


def _by_id(recall):
    return lambda n: [r[0] for r in recall.store.db.execute(
        "SELECT id FROM chunks ORDER BY id").fetchall()][:n]


def test_turn_copies_share_one_candidate_cap(tmp_path):
    """The per-turn candidate cap counts distinct pieces of the turn, whichever
    copy of the session they come from: a resumed copy of a piece already in
    the pool is not a new piece, so two copies of a long turn get neither twice
    the pool nor a pool eaten by their own duplicates."""
    recall = _resumed_copies(tmp_path)
    chunks = {}
    kept = recall._pool(_by_id(recall), 10, chunks, 100)
    assert len(kept) == 2  # A and B once each, not 2 copies x 2 pieces
    assert len({chunks[cid].content_hash for cid in kept}) == 2


def test_every_distinct_piece_reaches_the_pool_despite_copies(tmp_path):
    """Codex review 2026-11-08: with copies counted as pieces, the order
    A(orig) A(res) B(orig) B(res) C(orig) C(res) filled the cap of 3 with
    A, A, B and the reranker never saw C."""
    store = Store(tmp_path / "three.db")
    for i, text in enumerate(["piece A", "piece B", "piece C"]):
        for sid in ("orig", "res"):
            store.add(Chunk(session_id=sid, uuid="t1", role="assistant", text=text,
                            project="p", cwd="/x", git_branch="main", ts=T0,
                            file_path=f"/t/{sid}.jsonl", byte_offset=0, byte_len=1,
                            turn_index=1, content_hash=f"h{i}"), None)
    store.commit()
    recall = Recall(store, FakeEmbedder(), FakeReranker())
    chunks = {}
    kept = recall._pool(_by_id(recall), 10, chunks, 100)
    assert sorted(chunks[cid].content_hash for cid in kept) == ["h0", "h1", "h2"]


def test_cursor_bubble_ids_are_turns_within_their_composer(tmp_path):
    """Cursor bubble ids are only known to be unique inside one composer, so
    two composers reusing an id are two turns, not a resumed copy."""
    store = Store(tmp_path / "cursor.db")
    emb = FakeEmbedder()
    for sid, text in (("c1", "The Redis cache fix landed."), ("c2", "The disk cache fix landed.")):
        store.add(Chunk(session_id=sid, uuid="b0", role="assistant", text=text,
                        project="p", cwd="/x", git_branch="main", ts=T0,
                        file_path=f"/c/{sid}", byte_offset=0, byte_len=1, turn_index=1,
                        content_hash=hashlib.sha256(text.encode()).hexdigest(),
                        source="cursor"), emb.embed_documents([text])[0])
    store.commit()
    hits = Recall(store, emb, FakeReranker()).recall_search("cache fix landed", k=10, per_session=0)
    assert sorted(h.session_id for h in hits) == ["c1", "c2"]
