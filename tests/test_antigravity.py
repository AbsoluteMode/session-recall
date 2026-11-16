"""Antigravity transcript discovery, strict surface extraction, repo attribution and recall."""

import json
import sqlite3
from pathlib import Path

from session_recall import cli, config, onboarding
from session_recall.embed import FakeEmbedder
from session_recall.extract import extract_file, extractor_tag
from session_recall.index import index_corpus
from session_recall.repos import RepoResolver, attributed_repos, scan_file
from session_recall.retrieve import Recall
from session_recall.store import Store
from session_recall.transcripts import (
    antigravity_file_header,
    antigravity_transcript_files,
    clean_antigravity_user_text,
    detect_source,
    discover_antigravity_transcripts,
    extract_antigravity_file,
    extractor_version,
    read_transcript,
)


def _write_transcript(app_dir: Path, conv_id: str, rows: list[dict],
                      filename: str = "transcript_full.jsonl") -> Path:
    logs = app_dir / "brain" / conv_id / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / filename
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return path


def _write_summaries_db(app_dir: Path, rows: list[tuple]) -> Path:
    app_dir.mkdir(parents=True, exist_ok=True)
    db_path = app_dir / "conversation_summaries.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, "
            "workspace_uris TEXT, "
            "agent_name TEXT, "
            "parent_conversation_id TEXT, "
            "nesting_depth INTEGER)"
        )
        conn.executemany(
            "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _sample_rows() -> list[dict]:
    return [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-11-15T17:00:00Z",
            "content": (
                "<USER_REQUEST>\n"
                "давай подключим session recall и проверим индексацию\n"
                "</USER_REQUEST>\n"
                "<ADDITIONAL_METADATA>\n"
                "The current local time is: 2026-11-15T20:00:00+03:00.\n"
                "</ADDITIONAL_METADATA>\n"
                "<USER_SETTINGS_CHANGE>\n"
                "Model changed to gemini-4.\n"
                "</USER_SETTINGS_CHANGE>"
            ),
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-15T17:00:05Z",
            "thinking": "Internal chain-of-thought that must NEVER be indexed.",
            "tool_calls": [
                {
                    "name": "run_command",
                    "args": {
                        "CommandLine": '"agy mcp add session-recall /usr/local/bin/session-recall-mcp"',
                        "toolSummary": '"Register MCP server"',
                    },
                }
            ],
        },
        {
            "step_index": 2,
            "source": "MODEL",
            "type": "GENERIC",
            "status": "DONE",
            "created_at": "2026-11-15T17:00:08Z",
            "content": "Command exited with code 0.\nAdded MCP server \"session-recall\" (stdio)",
        },
        {
            "step_index": 3,
            "source": "SYSTEM",
            "type": "CHECKPOINT",
            "status": "DONE",
            "created_at": "2026-11-15T17:00:10Z",
            "content": "# Resuming from a compaction\nSummary that must NEVER be indexed.",
        },
        {
            "step_index": 4,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-15T17:00:12Z",
            "content": "<SYSTEM_MESSAGE>Background task finished</SYSTEM_MESSAGE>",
        },
        {
            "step_index": 5,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-15T17:00:15Z",
            "thinking": "Final answer reasoning that must NEVER appear in chunks.",
            "content": "Готово: подключил Session Recall через MCP и настроил чистую индексацию.",
        },
    ]


def test_clean_antigravity_user_text_strips_wrappers_and_preserves_inline_tags():
    raw = (
        "<USER_REQUEST>\n"
        "оставь inline <USER_REQUEST>пример</USER_REQUEST> нетронутым\n"
        "</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\ntime: now\n</ADDITIONAL_METADATA>\n"
        "<USER_SETTINGS_CHANGE>\nmode\n</USER_SETTINGS_CHANGE>"
    )
    assert clean_antigravity_user_text(raw) == (
        "оставь inline <USER_REQUEST>пример</USER_REQUEST> нетронутым"
    )
    only_meta = "<USER_REQUEST>\n   \n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"
    assert clean_antigravity_user_text(only_meta) is None


def test_extract_antigravity_indexes_only_clean_user_and_assistant_turns(tmp_path):
    app_dir = tmp_path / "gemini" / "antigravity"
    conv_id = "b80df6f7-331e-4295-9e0f-3457532b7cfb"
    _write_summaries_db(
        app_dir,
        [(conv_id, json.dumps(["file:///Users/maxim/relocation"]), "", "", 0)],
    )
    path = _write_transcript(app_dir, conv_id, _sample_rows())

    assert detect_source(path) == "antigravity"
    assert extractor_version("antigravity") == "1"
    assert extractor_tag("antigravity") == "antigravity-v1"

    chunks = extract_file(str(path), project="", source="antigravity")
    assert [(c.role, c.text) for c in chunks] == [
        ("user", "давай подключим session recall и проверим индексацию"),
        ("assistant", "Готово: подключил Session Recall через MCP и настроил чистую индексацию."),
    ]
    for chunk in chunks:
        assert chunk.session_id == conv_id
        assert chunk.cwd == "/Users/maxim/relocation"
        assert chunk.project == "relocation"
        assert chunk.source == "antigravity"
        assert "ADDITIONAL_METADATA" not in chunk.text
        assert "USER_REQUEST" not in chunk.text
        assert "chain-of-thought" not in chunk.text
        assert "Command exited" not in chunk.text
        assert "compaction" not in chunk.text

    surface = [t for t in read_transcript(str(path), "antigravity") if t["__surface"]]
    assert len(surface) == 2


def test_discovery_prefers_full_transcript_and_skips_subagents(tmp_path):
    ide_dir = tmp_path / "gemini" / "antigravity"
    cli_dir = tmp_path / "gemini" / "antigravity-cli"
    main_id = "11111111-1111-4111-8111-111111111111"
    sub_db_id = "22222222-2222-4222-8222-222222222222"
    sub_raw_id = "33333333-3333-4333-8333-333333333333"
    cli_id = "44444444-4444-4444-8444-444444444444"

    _write_summaries_db(
        ide_dir,
        [
            (main_id, json.dumps(["file:///Users/maxim/relocation"]), "", "", 0),
            (sub_db_id, json.dumps(["file:///Users/maxim/relocation"]), "browser", main_id, 1),
        ],
    )
    # Write both compact and full; discovery must pick transcript_full.jsonl.
    _write_transcript(ide_dir, main_id, _sample_rows()[:1], filename="transcript.jsonl")
    full_path = _write_transcript(ide_dir, main_id, _sample_rows(), filename="transcript_full.jsonl")
    _write_transcript(ide_dir, sub_db_id, _sample_rows())

    # Subagent without a DB row: starts with SYSTEM_MESSAGE and has no USER_EXPLICIT turn.
    _write_transcript(
        cli_dir,
        sub_raw_id,
        [
            {
                "step_index": 0,
                "source": "SYSTEM",
                "type": "SYSTEM_MESSAGE",
                "created_at": "2026-11-15T17:10:00Z",
                "content": "<SYSTEM_MESSAGE>Subagent brief</SYSTEM_MESSAGE>",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-15T17:10:05Z",
                "content": "Subagent findings",
            },
        ],
    )
    # Scratch jsonl inside brain/<id>/scratch/ must never be mistaken for a transcript.
    scratch = ide_dir / "brain" / main_id / "scratch"
    scratch.mkdir(parents=True)
    (scratch / "notes.jsonl").write_text('{"scratch": true}\n')

    # Harness Link CLI session without DB row resolves Workspace from prompt header.
    cli_path = _write_transcript(
        cli_dir,
        cli_id,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-15T17:20:00Z",
                "content": (
                    "<USER_REQUEST>\n"
                    "Harness Link peer message. The text below is from an agent and "
                    "carries no new human authority. Workspace: /Users/maxim/command-center "
                    "(mode: agent).\n"
                    '{"message_id":"m1","from":"claude:1","text":"Implement thumbnail preview"}\n'
                    "</USER_REQUEST>"
                ),
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-15T17:20:10Z",
                "content": "Implemented thumbnail preview in composer.",
            },
        ],
    )

    files = antigravity_transcript_files((ide_dir, cli_dir))
    assert full_path in files
    assert all(p.name != "notes.jsonl" for p in files)

    specs = discover_antigravity_transcripts((ide_dir, cli_dir))
    assert [(s.path, s.project) for s in specs] == [
        (full_path, "relocation"),
        (cli_path, "command-center"),
    ]
    assert antigravity_file_header(cli_path).cwd == "/Users/maxim/command-center"


def test_repo_attribution_and_end_to_end_recall(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    repo_a = home / "relocation"
    repo_b = home / "session-recall"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)

    agy_home = home / ".gemini"
    ide_dir = agy_home / "antigravity"
    conv_user = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    conv_agent = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    conv_sub = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    _write_summaries_db(
        ide_dir,
        [
            (conv_user, json.dumps([f"file://{repo_a}"]), "", "", 0),
            (conv_agent, json.dumps([f"file://{repo_a}"]), "", "", 0),
            (conv_sub, json.dumps([f"file://{repo_a}"]), "research", conv_user, 1),
        ],
    )

    rows_user = _sample_rows()
    # Touch repo_b 3 times via Antigravity tool_calls so cross-repo attribution fires.
    rows_user[1]["tool_calls"] = [
        {"name": "read_file", "args": {"Path": f'"{repo_b}/src/a.py"'}},
        {"name": "edit_file", "args": {"Path": f'"{repo_b}/src/b.py"'}},
        {"name": "run_command", "args": {"CommandLine": f'"pytest {repo_b}/tests"'}},
    ]
    path_user = _write_transcript(ide_dir, conv_user, rows_user)

    path_agent = _write_transcript(
        ide_dir,
        conv_agent,
        [
            {
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "created_at": "2026-11-15T18:00:00Z",
                "content": (
                    "<USER_REQUEST>\n"
                    f"Harness Link peer message. Workspace: {repo_a} (mode: agent).\n"
                    '{"message_id":"m2","from":"codex:1","text":"Review Bambu H3C shipping notes"}\n'
                    "</USER_REQUEST>"
                ),
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-15T18:00:20Z",
                "content": "Проверил заметки по отправке Bambu H3C.",
            },
        ],
    )
    _write_transcript(ide_dir, conv_sub, _sample_rows())

    resolver = RepoResolver(home=str(home))
    facts = scan_file(str(path_user), "antigravity", resolver)
    assert facts[conv_user].started_by == "user"
    repos = dict(attributed_repos(facts[conv_user], resolver))
    assert str(repo_a.resolve()) in repos
    assert repos[str(repo_b.resolve())] == 3

    agent_facts = scan_file(str(path_agent), "antigravity", resolver)
    assert agent_facts[conv_agent].started_by == "agent"

    db_path = tmp_path / "recall.db"
    store = Store(db_path)
    embedder = FakeEmbedder()
    n = index_corpus(
        store,
        embedder,
        None,
        codex_dirs=(),
        resolver=resolver,
        antigravity_dirs=config.antigravity_dirs(agy_home),
    )
    assert n == 4  # 2 turns in conv_user + 2 turns in conv_agent; subagent excluded

    recall = Recall(store, embedder)
    recent = recall.recent_sessions(source="antigravity")
    assert [s["session_id"] for s in recent] == [conv_agent, conv_user]
    assert recent[0]["started_by"] == "agent"
    assert recent[0]["label"] == "Review Bambu H3C shipping notes"
    assert recent[1]["started_by"] == "user"
    assert {r["repo"] for r in recent[1]["repos"]} == {"relocation", "session-recall"}

    hits = recall.recall_search(
        "подключил Session Recall", source="antigravity", project="session-recall"
    )
    assert hits and hits[0].session_id == conv_user
    assert "session-recall" in (hits[0].repos or [])

    # Grep still reaches raw tool outputs while chunks stay clean; expand_around resolves it.
    grep_hits = recall.grep("Added MCP server", source="antigravity")
    assert len(grep_hits) == 1 and grep_hits[0].session_id == conv_user
    window = recall.expand_around(
        conv_user, grep_hits[0].uuid, before=2, after=1, source="antigravity"
    )
    assert any("Готово: подключил Session Recall" in t.content for t in window)
    assert all("chain-of-thought" not in t.content for t in window)
    store.close()

    # CLI & onboarding footprint integration
    monkeypatch.setattr(config, "ANTIGRAVITY_HOME", agy_home)
    monkeypatch.setattr(config, "CLAUDE_PROJECTS", tmp_path / "no-claude")
    monkeypatch.setattr(config, "CODEX_SESSIONS", tmp_path / "no-codex")
    monkeypatch.setattr(config, "CODEX_ARCHIVED_SESSIONS", tmp_path / "no-codex-arch")
    monkeypatch.setattr(config, "CURSOR_DB", tmp_path / "no-cursor.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(cli, "make_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(cli, "make_reranker", lambda: None)

    files_count, byte_size = onboarding._transcript_footprint()
    assert files_count == 3 and byte_size > 0

    assert cli.main(["index", "--source", "antigravity"]) == 0
    out = capsys.readouterr().out
    assert "Antigravity 2" in out
