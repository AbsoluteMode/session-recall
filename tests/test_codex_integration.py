import json
import shutil
from pathlib import Path

from session_recall.embed import FakeEmbedder
from session_recall.index import index_corpus
from session_recall.retrieve import Recall
from session_recall.store import Store


FIXTURES = Path(__file__).parent / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex_session.jsonl.fixture"
CLAUDE_FIXTURE = FIXTURES / "session_a.jsonl"


def _write_codex_session(
    path: Path,
    *,
    session_id: str,
    cwd: str,
    user_text: str,
    assistant_text: str,
    day: int = 2,
    meta_extra: dict | None = None,
    tool_output: str | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "id": session_id,
        "cwd": cwd,
        "source": "vscode",
        "thread_source": "user",
        "git": {"branch": "feature/codex-recall"},
    }
    meta.update(meta_extra or {})
    prefix = f"2026-07-{day:02d}T10:00:"
    rows = [
        {"timestamp": prefix + "00Z", "type": "session_meta", "payload": meta},
        {"timestamp": prefix + "01Z", "type": "event_msg", "payload": {
            "type": "user_message", "message": user_text,
        }},
        {"timestamp": prefix + "02Z", "type": "event_msg", "payload": {
            "type": "agent_message", "message": assistant_text, "phase": "final",
        }},
    ]
    if tool_output is not None:
        rows.extend([
            {"timestamp": prefix + "03Z", "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command",
                "arguments": json.dumps({"cmd": "synthetic integration command"}),
                "call_id": "call-integration",
            }},
            {"timestamp": prefix + "04Z", "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "call-integration",
                "output": tool_output,
            }},
        ])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def _claude_corpus(tmp_path: Path) -> Path:
    project = tmp_path / "claude-projects" / "-Users-me-proj"
    project.mkdir(parents=True)
    shutil.copy(CLAUDE_FIXTURE, project / "session_a.jsonl")
    return tmp_path / "claude-projects"


def _recall(store: Store) -> Recall:
    return Recall(store, FakeEmbedder(), None)


def test_index_mixes_claude_active_codex_and_archived_codex(tmp_path):
    claude = _claude_corpus(tmp_path)
    active = tmp_path / "codex" / "sessions"
    archive = tmp_path / "codex" / "archived_sessions"
    active_file = active / "2026" / "07" / "01" / "rollout-active.jsonl"
    active_file.parent.mkdir(parents=True)
    shutil.copy(CODEX_FIXTURE, active_file)
    archived_file = _write_codex_session(
        archive / "rollout-archived.jsonl",
        session_id="codex-archived",
        cwd="/Users/me/archive-repo",
        user_text="Archived Codex integration question",
        assistant_text="Archived Codex integration answer",
        day=3,
    )

    store = Store(tmp_path / "mixed.db")
    embedder = FakeEmbedder()
    assert index_corpus(store, embedder, claude, (active, archive)) == 8
    assert store.db.execute(
        "SELECT source, count(*) FROM chunks GROUP BY source ORDER BY source"
    ).fetchall() == [("claude", 2), ("codex", 6)]
    assert store.indexed_source(str(active_file)) == "codex"
    assert store.indexed_source(str(archived_file)) == "codex"
    assert index_corpus(store, embedder, claude, (active, archive)) == 0
    store.close()


def test_index_skips_every_codex_subagent_marker(tmp_path):
    active = tmp_path / "sessions"
    main = _write_codex_session(
        active / "2026" / "07" / "01" / "main.jsonl",
        session_id="main-session",
        cwd="/workspace/main",
        user_text="Main session question",
        assistant_text="Main session answer",
    )
    sidechains = [
        _write_codex_session(
            active / "2026" / "07" / "01" / "thread-source.jsonl",
            session_id="child-thread-source",
            cwd="/workspace/main",
            user_text="thread_source child text",
            assistant_text="thread_source child answer",
            meta_extra={"thread_source": "subagent"},
        ),
        _write_codex_session(
            active / "2026" / "07" / "01" / "source-object.jsonl",
            session_id="child-source-object",
            cwd="/workspace/main",
            user_text="source object child text",
            assistant_text="source object child answer",
            meta_extra={"source": {"subagent": {"parent": "main-session"}}},
        ),
        _write_codex_session(
            active / "2026" / "07" / "01" / "agent-path.jsonl",
            session_id="child-agent-path",
            cwd="/workspace/main",
            user_text="agent_path child text",
            assistant_text="agent_path child answer",
            meta_extra={"agent_path": "/root/worker"},
        ),
    ]

    store = Store(tmp_path / "subagents.db")
    assert index_corpus(
        store,
        FakeEmbedder(),
        tmp_path / "missing-claude",
        (active, tmp_path / "missing-archive"),
    ) == 2
    assert store.db.execute(
        "SELECT session_id FROM chunks GROUP BY session_id"
    ).fetchall() == [("main-session",)]
    assert store.indexed_source(str(main)) == "codex"
    assert all(store.indexed_source(str(path)) is None for path in sidechains)
    store.close()


def test_search_and_raw_grep_cursors_expand_and_step(tmp_path):
    active = tmp_path / "sessions"
    transcript = active / "2026" / "07" / "01" / "rollout-codex-s1.jsonl"
    transcript.parent.mkdir(parents=True)
    shutil.copy(CODEX_FIXTURE, transcript)
    store = Store(tmp_path / "retrieval.db")
    index_corpus(store, FakeEmbedder(), None, (active, tmp_path / "missing-archive"))
    recall = _recall(store)

    search_hit = next(
        hit for hit in recall.recall_search("durable SQLite cache", k=10)
        if hit.source == "codex" and "durable SQLite" in hit.snippet
    )
    search_window = recall.expand_around(
        search_hit.session_id, search_hit.uuid, before=0, after=1
    )
    assert search_window and "durable SQLite cache" in search_window[0].content
    search_next = recall.step(search_hit.session_id, search_hit.uuid, "next")
    assert search_next and "Preserve vectors" in search_next[0].content

    grep_hit = next(hit for hit in recall.grep("81 passed") if hit.source == "codex")
    assert "81 passed" in grep_hit.snippet
    grep_window = recall.expand_around(
        grep_hit.session_id, grep_hit.uuid, before=1, after=0
    )
    assert any("81 passed" in turn.content for turn in grep_window)
    assert any("exec_command" in turn.content for turn in grep_window)
    previous = recall.step(grep_hit.session_id, grep_hit.uuid, "prev")
    assert previous and "exec_command" in previous[0].content

    assert recall.grep("ciphertext-must-never-escape", source="codex") == []
    safe_reasoning = recall.grep("Preserve vectors", source="codex")
    assert safe_reasoning and all(
        "ciphertext-must-never-escape" not in hit.snippet for hit in safe_reasoning)
    store.close()


def test_chunkless_codex_grep_anchor_falls_back_to_rollout_filename(tmp_path):
    active = tmp_path / "sessions"
    session_id = "019f0000-1111-7222-8333-444455556666"
    path = active / "2026" / "07" / "04" / (
        f"rollout-2026-07-04T10-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True)
    rows = [
        {"timestamp": "2026-07-04T10:00:00Z", "type": "session_meta", "payload": {
            "id": session_id, "cwd": "/workspace/chunkless", "source": "vscode",
        }},
        {"timestamp": "2026-07-04T10:00:01Z", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command",
            "arguments": json.dumps({"cmd": "chunkless synthetic command"}),
            "call_id": "chunkless-call",
        }},
        {"timestamp": "2026-07-04T10:00:02Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "chunkless-call",
            "output": "CHUNKLESS_CODEX_OUTPUT_NEEDLE",
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    store = Store(tmp_path / "chunkless.db")
    assert index_corpus(store, FakeEmbedder(), None, (active,)) == 0
    assert store.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
    assert store.indexed_source(str(path)) == "codex"
    recall = _recall(store)

    hit = recall.grep(
        "CHUNKLESS_CODEX_OUTPUT_NEEDLE",
        scope_cwd="/workspace/chunkless",
    )[0]
    assert hit.session_id == session_id and hit.uuid.startswith(f"codex:{session_id}:")
    expanded = recall.expand_around(hit.session_id, hit.uuid, before=1, after=0)
    assert any("CHUNKLESS_CODEX_OUTPUT_NEEDLE" in turn.content for turn in expanded)
    previous = recall.step(hit.session_id, hit.uuid, "prev")
    assert previous and "chunkless synthetic command" in previous[0].content
    store.close()


def test_scope_source_and_recent_filters_span_mixed_corpus(tmp_path):
    claude = _claude_corpus(tmp_path)
    active = tmp_path / "sessions"
    _write_codex_session(
        active / "2026" / "07" / "02" / "repo-a.jsonl",
        session_id="codex-repo-a",
        cwd="/workspace/repo-a",
        user_text="Shared scopable memory alpha",
        assistant_text="Scoped answer alpha",
        day=2,
        tool_output="RAW_SCOPE_MARKER alpha",
    )
    _write_codex_session(
        active / "2026" / "07" / "05" / "repo-b.jsonl",
        session_id="codex-repo-b",
        cwd="/workspace/repo-b",
        user_text="Shared scopable memory beta",
        assistant_text="Scoped answer beta",
        day=5,
        tool_output="RAW_SCOPE_MARKER beta",
    )
    store = Store(tmp_path / "scope.db")
    index_corpus(store, FakeEmbedder(), claude, (active, tmp_path / "missing-archive"))
    recall = _recall(store)

    # Unified is the default contract: no source filter searches both hosts,
    # while every result keeps its provenance marker.
    unified_recent = recall.recent_sessions(limit=10, now=2_000_000_000)
    assert {item["source"] for item in unified_recent} == {"claude", "codex"}

    scoped = recall.recall_search(
        "shared scopable memory",
        k=10,
        scope_cwd="/workspace/repo-a",
        source="codex",
    )
    assert scoped and {hit.session_id for hit in scoped} == {"codex-repo-a"}
    assert all(hit.source == "codex" for hit in scoped)
    assert recall.recall_search("cache embeddings", k=5, source="claude")

    grep_hits = recall.grep(
        "RAW_SCOPE_MARKER",
        scope_cwd="/workspace/repo-a",
        source="codex",
    )
    assert grep_hits and {hit.session_id for hit in grep_hits} == {"codex-repo-a"}

    recent = recall.recent_sessions(source="codex", limit=10, now=2_000_000_000)
    assert [item["session_id"] for item in recent] == ["codex-repo-b", "codex-repo-a"]
    assert all(item["source"] == "codex" for item in recent)
    assert recent[0]["label"] == "Shared scopable memory beta"
    scoped_recent = recall.recent_sessions(
        scope_cwd="/workspace/repo-a", source="codex", limit=10, now=2_000_000_000
    )
    assert [item["session_id"] for item in scoped_recent] == ["codex-repo-a"]
    store.close()


def test_missing_claude_and_codex_roots_are_a_noop(tmp_path):
    store = Store(tmp_path / "missing.db")
    assert index_corpus(
        store,
        FakeEmbedder(),
        tmp_path / "missing-claude",
        (tmp_path / "missing-active", tmp_path / "missing-archive"),
    ) == 0
    assert store.db.execute("SELECT count(*) FROM indexed_files").fetchone()[0] == 0
    store.close()


def test_archive_move_reuses_vectors_and_rekeys_the_path(tmp_path):
    class CountingEmbedder(FakeEmbedder):
        def __init__(self):
            super().__init__()
            self.texts_seen = []

        def embed_documents(self, texts):
            self.texts_seen.extend(texts)
            return super().embed_documents(texts)

    active = tmp_path / "sessions"
    archive = tmp_path / "archived_sessions"
    old = _write_codex_session(
        active / "2026" / "07" / "02" / "moving.jsonl",
        session_id="moving-session",
        cwd="/workspace/moving",
        user_text="Archive this memory",
        assistant_text="Archive move keeps vectors",
    )
    store = Store(tmp_path / "moving.db")
    embedder = CountingEmbedder()
    index_corpus(store, embedder, None, (active, archive))
    assert embedder.texts_seen

    archive.mkdir(parents=True)
    moved = archive / "moving.jsonl"
    old.rename(moved)
    embedder.texts_seen.clear()
    assert index_corpus(store, embedder, None, (active, archive)) == 2
    assert embedder.texts_seen == [], "archive move should reuse same-session vectors"
    assert store.indexed_source(str(old)) is None
    assert store.indexed_source(str(moved)) == "codex"
    store.close()


def test_failed_archive_reindex_keeps_old_memory_until_retry(tmp_path):
    class PoisonEmbedder(FakeEmbedder):
        def embed_documents(self, texts):
            if any("poison" in text for text in texts):
                raise RuntimeError("synthetic provider failure")
            return super().embed_documents(texts)

    active = tmp_path / "sessions"
    archive = tmp_path / "archived_sessions"
    old = _write_codex_session(
        active / "2026" / "07" / "02" / "moving.jsonl",
        session_id="moving-failure",
        cwd="/workspace/moving",
        user_text="Original durable memory",
        assistant_text="Original answer",
    )
    store = Store(tmp_path / "moving-failure.db")
    index_corpus(store, FakeEmbedder(), None, (active, archive))

    archive.mkdir(parents=True)
    moved = archive / "moving.jsonl"
    old.rename(moved)
    with moved.open("a") as handle:
        handle.write(json.dumps({
            "timestamp": "2026-07-02T10:00:10Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "poison new tail"},
        }) + "\n")
    index_corpus(store, PoisonEmbedder(), None, (active, archive))

    assert store.fts("Original", 5, source="codex"), \
        "failed replacement must not erase the previous good memory"
    assert store.indexed_source(str(old)) == "codex"
    assert store.indexed_source(str(moved)) is None
    store.close()


def test_source_selective_index_prunes_only_the_selected_source(tmp_path):
    active = tmp_path / "sessions"
    archive = tmp_path / "archived_sessions"
    archive.mkdir(parents=True)
    path = _write_codex_session(
        active / "2026" / "07" / "02" / "codex.jsonl",
        session_id="codex-delete",
        cwd="/workspace/codex",
        user_text="Codex memory to delete",
        assistant_text="Codex answer",
    )
    store = Store(tmp_path / "selective.db")
    index_corpus(store, FakeEmbedder(), None, (active, archive))
    path.unlink()

    claude_root = tmp_path / "claude-projects"
    claude_root.mkdir()
    index_corpus(store, FakeEmbedder(), claude_root, ())
    assert store.indexed_source(str(path)) == "codex"

    index_corpus(store, FakeEmbedder(), None, (active, archive))
    assert store.indexed_source(str(path)) is None
    store.close()


def test_missing_previously_indexed_codex_root_is_not_pruned(tmp_path):
    active = tmp_path / "sessions"
    archive = tmp_path / "archived_sessions"
    archived = _write_codex_session(
        archive / "archived.jsonl",
        session_id="temporarily-unavailable",
        cwd="/workspace/archive",
        user_text="Keep this archived memory",
        assistant_text="It remains until storage returns",
    )
    active.mkdir()
    store = Store(tmp_path / "unavailable-root.db")
    index_corpus(store, FakeEmbedder(), None, (active, archive))

    offline = tmp_path / "archived_sessions.offline"
    archive.rename(offline)
    index_corpus(store, FakeEmbedder(), None, (active, archive))

    assert store.indexed_source(str(archived)) == "codex"
    assert store.fts("archived", 5, source="codex")
    store.close()
