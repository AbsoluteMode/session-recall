import json
from pathlib import Path
import shutil

import pytest

from session_recall.transcripts import (
    TranscriptSpec,
    codex_file_header,
    codex_file_meta,
    discover_codex_transcripts,
    extract_codex_file,
    extractor_version,
    read_transcript,
)


FIX = Path(__file__).parent / "fixtures" / "codex_session.jsonl.fixture"


def _text(turn):
    content = turn["message"]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def test_discovery_finds_active_and_archived_but_skips_subagents(tmp_path):
    sessions = tmp_path / "sessions"
    archived = tmp_path / "archived_sessions"
    active = sessions / "2026" / "07" / "01" / "active.jsonl"
    active.parent.mkdir(parents=True)
    archived.mkdir()
    archived_file = archived / "archived.jsonl"
    shutil.copy(FIX, active)
    shutil.copy(FIX, archived_file)

    (active.parent / "subagent-string.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "child-1", "cwd": "/child", "source": "subagent"},
    }) + "\n")
    (active.parent / "subagent-dict.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "child-2", "cwd": "/child",
                    "source": {"subagent": {"parent": "root"}}},
    }) + "\n")
    (active.parent / "subagent-thread.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "child-3", "cwd": "/child",
                    "thread_source": "subagent"},
    }) + "\n")

    specs = discover_codex_transcripts(sessions, archived)

    assert specs == [
        TranscriptSpec(path=archived_file, project="repo-2", source="codex"),
        TranscriptSpec(path=active, project="repo-2", source="codex"),
    ]


def test_repeated_session_meta_keeps_first_id_and_updates_cwd():
    meta = codex_file_meta(FIX)
    assert meta["session_id"] == "codex-s1"
    assert meta["cwd"] == "/Users/me/repo-2"
    assert meta["is_subagent"] is False


def test_codex_normalization_prefers_visible_events_and_preserves_phase():
    turns = read_transcript(str(FIX), "codex")
    surface = [turn for turn in turns if turn["__surface"]]

    assert [(turn["message"]["role"], _text(turn)) for turn in surface] == [
        ("user", "How do we cache vectors?"),
        ("assistant", "Use a durable SQLite cache."),
        ("user", "What about archived sessions?"),
        ("assistant", "Scan the archived rollout directory too."),
    ]
    assert [turn.get("phase") for turn in surface] == [None, "commentary", None, "final"]
    assert all(turn["sessionId"] == "codex-s1" for turn in turns)
    assert surface[0]["cwd"] == "/Users/me/repo"
    assert surface[-1]["cwd"] == "/Users/me/repo-2"

    uuids = [turn["uuid"] for turn in turns]
    assert len(uuids) == len(set(uuids))
    assert all(uuid.startswith("codex:codex-s1:") for uuid in uuids)


def test_codex_normalization_renders_tools_and_safe_reasoning():
    turns = read_transcript(str(FIX), "codex")
    serialized = json.dumps(turns)
    assert "ciphertext-must-never-escape" not in serialized
    assert "encrypted_content" not in serialized

    thinking = next(
        block
        for turn in turns
        for block in turn["message"]["content"]
        if isinstance(turn["message"]["content"], list)
        and isinstance(block, dict) and block.get("type") == "thinking"
    )
    assert thinking["thinking"] == "Preserve vectors for unchanged chunks."

    tool_use = next(
        block
        for turn in turns
        for block in turn["message"]["content"]
        if isinstance(turn["message"]["content"], list)
        and isinstance(block, dict) and block.get("type") == "tool_use"
    )
    assert tool_use == {
        "type": "tool_use", "name": "exec_command", "input": {"cmd": "pytest"},
    }
    tool_result = next(
        block
        for turn in turns
        for block in turn["message"]["content"]
        if isinstance(turn["message"]["content"], list)
        and isinstance(block, dict) and block.get("type") == "tool_result"
    )
    assert tool_result["content"] == "81 passed"


def test_response_messages_remain_available_but_never_become_surface(tmp_path):
    path = tmp_path / "fallback.jsonl"
    rows = [
        {"timestamp": "2026-07-01T10:00:00Z", "type": "session_meta",
         "payload": {"id": "fallback-s", "cwd": "/repo", "source": "cli"}},
        {"timestamp": "2026-07-01T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text":
              "<environment_context>noise</environment_context>\nFallback question"},
         ]}},
        {"timestamp": "2026-07-01T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": "Fallback question"},
         ]}},
        {"timestamp": "2026-07-01T10:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [
             {"type": "output_text", "text": "Fallback answer"},
         ], "phase": "final"}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    turns = read_transcript(str(path), "codex")
    assert not any(turn["__surface"] for turn in turns)
    assert [(_text(turn), turn.get("phase")) for turn in turns[1:]] == [
        ("Fallback question", None),
        ("Fallback question", None),
        ("Fallback answer", "final"),
    ]


def test_canonical_user_message_is_preserved_verbatim(tmp_path):
    path = tmp_path / "canonical.jsonl"
    message = "<environment_context>part of my prompt</environment_context>\nExplain it."
    rows = [
        {"type": "session_meta", "payload": {"id": "canonical-s", "cwd": "/repo"}},
        {"timestamp": "2026-07-01T10:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": message}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    chunks = extract_codex_file(str(path))
    assert [chunk.text for chunk in chunks] == [message]


def test_header_finds_metadata_after_preamble_and_prefers_filename_uuid(tmp_path):
    filename_id = "11111111-2222-3333-4444-555555555555"
    path = tmp_path / f"rollout-2026-07-01T00-00-00-{filename_id}.jsonl"
    rows = [
        {"type": "future_preamble", "payload": {"format": 2}},
        {"type": "session_meta", "payload": {
            "session_id": "parent-session", "cwd": "/repo", "source": "cli",
        }},
    ]
    path.write_text("not-json\n" + "".join(json.dumps(row) + "\n" for row in rows))

    meta = codex_file_header(path)
    assert meta.session_id == filename_id
    assert meta.cwd == "/repo"


def test_extract_codex_file_returns_only_surface_chunks_with_offsets():
    chunks = extract_codex_file(str(FIX), project="fallback-project")
    assert [(chunk.role, chunk.text) for chunk in chunks] == [
        ("user", "How do we cache vectors?"),
        ("assistant", "Use a durable SQLite cache."),
        ("user", "What about archived sessions?"),
        ("assistant", "Scan the archived rollout directory too."),
    ]
    assert all(chunk.source == "codex" for chunk in chunks)
    assert all(chunk.session_id == "codex-s1" for chunk in chunks)
    assert [chunk.project for chunk in chunks] == ["repo", "repo", "repo-2", "repo-2"]
    assert all(chunk.ts > 0 and chunk.content_hash for chunk in chunks)

    data = FIX.read_bytes()
    for chunk in chunks:
        raw = data[chunk.byte_offset:chunk.byte_offset + chunk.byte_len]
        assert json.loads(raw)["timestamp"]
        assert chunk.uuid == f"codex:codex-s1:{chunk.byte_offset}"


def test_claude_passthrough_and_version_validation(tmp_path):
    path = tmp_path / "claude.jsonl"
    raw = {"type": "user", "uuid": "u1", "sessionId": "s1",
           "message": {"role": "user", "content": "hello"}}
    path.write_text(json.dumps(raw) + "\n")
    assert read_transcript(str(path), "claude") == [raw]
    assert extractor_version("claude") == "2"
    assert extractor_version("codex") == "1"
    assert extractor_version("cursor") == "2"
    with pytest.raises(ValueError, match="unknown transcript source"):
        read_transcript(str(path), "other")
