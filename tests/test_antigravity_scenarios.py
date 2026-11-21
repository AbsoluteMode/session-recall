"""Antigravity edge cases from real use: broken/empty files, a huge session,
secrets in tool traffic, a canonical file that moves, and a mixed index."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from session_recall import config
from session_recall.embed import FakeEmbedder
from session_recall.hub.client import local_files
from session_recall.index import index_corpus
from session_recall.retrieve import Recall
from session_recall.store import Store
from session_recall.transcripts import antigravity_transcript_files

FIXTURES = Path(__file__).parent / "fixtures"
CONV = "12345678-1234-4234-8234-123456789abc"
FAKE_KEY = "sk-ant-api03-" + "Q" * 40 + "-fake"


def _write(app_dir: Path, conv_id: str, rows, filename="transcript_full.jsonl") -> Path:
    logs = app_dir / "brain" / conv_id / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / filename
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False) for r in rows]
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _db(app_dir: Path, rows) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(app_dir / "conversation_summaries.db"))
    conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, "
                 "workspace_uris TEXT, agent_name TEXT, parent_conversation_id TEXT, "
                 "nesting_depth INTEGER)")
    conn.executemany("INSERT INTO conversation_summaries VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def _user(i, text, ts="2026-11-15T17:00:00Z"):
    return {"step_index": i, "source": "USER_EXPLICIT", "type": "USER_INPUT",
            "created_at": ts, "content": f"<USER_REQUEST>\n{text}\n</USER_REQUEST>"}


def _reply(i, text, ts="2026-11-15T17:00:05Z"):
    return {"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE",
            "created_at": ts, "content": text}


def _tool(i, name, args, output, ts="2026-11-15T17:00:03Z"):
    return [
        {"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": ts,
         "thinking": "reasoning " + FAKE_KEY, "tool_calls": [{"name": name, "args": args}]},
        {"step_index": i + 1, "source": "MODEL", "type": "GENERIC", "created_at": ts,
         "content": output},
    ]


def _chunks(store: Store) -> list[tuple[str, str, str]]:
    return store.db.execute(
        "SELECT source, role, text FROM chunks ORDER BY id").fetchall()


def _index(store, roots, **kw):
    return index_corpus(store, FakeEmbedder(), kw.pop("claude", None),
                        codex_dirs=kw.pop("codex", ()), antigravity_dirs=roots, **kw)


def test_empty_and_broken_transcripts_index_without_failing(tmp_path, capsys):
    app = tmp_path / "antigravity"
    empty_id = "00000000-0000-4000-8000-000000000001"
    broken_id = "00000000-0000-4000-8000-000000000002"
    _db(app, [(empty_id, "[]", "", "", 0), (broken_id, "not json", "", "", 0)])
    _write(app, empty_id, [])
    _write(app, broken_id, [
        "{truncated", "[1, 2, 3]", '"just a string"', "\xff\xfe garbage",
        _user(3, "живой запрос после мусора"),
        {"step_index": 4, "source": "MODEL", "type": "PLANNER_RESPONSE"},  # no content
        {"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": 42},
        _reply(6, "живой ответ"),
        '{"step_index": 7, "source": "MODEL", "type": "PLANNER_RES',  # cut mid-write
    ])
    # A conversation dir with no logs at all is not a transcript.
    (app / "brain" / "00000000-0000-4000-8000-000000000003").mkdir(parents=True)

    store = Store(tmp_path / "r.db")
    n = _index(store, (app,))
    assert "failed to index" not in capsys.readouterr().err
    assert n == 2
    assert [(r, t) for _, r, t in _chunks(store)] == [
        ("user", "живой запрос после мусора"), ("assistant", "живой ответ")]
    # Both files are marked indexed, so the next run does no work.
    assert _index(store, (app,)) == 0
    assert store.db.execute("SELECT count(*) FROM indexed_files").fetchone()[0] == 2
    store.close()


def test_large_session_keeps_only_dialogue_and_splits_long_answers(tmp_path):
    app = tmp_path / "antigravity"
    _db(app, [(CONV, json.dumps(["file:///work/big"]), "", "", 0)])
    rows, step = [], 0
    for turn in range(150):
        rows.append(_user(step, f"вопрос номер {turn}")); step += 1
        for _ in range(4):
            rows += _tool(step, "run_command", {"CommandLine": "cat big.log"},
                          f"TOOLNOISE {turn} " + "x" * 6000)
            step += 2
        rows.append(_reply(step, f"ответ номер {turn}")); step += 1
    long_answer = " ".join(f"абзац{i} про индексацию большой сессии." for i in range(2500))
    rows.append(_reply(step, long_answer))
    path = _write(app, CONV, rows)
    assert path.stat().st_size > 3_000_000

    store = Store(tmp_path / "r.db")
    _index(store, (app,))
    chunks = _chunks(store)
    assert all("TOOLNOISE" not in t and "cat big.log" not in t for _, _, t in chunks)
    users = [t for _, r, t in chunks if r == "user"]
    assert users == [f"вопрос номер {i}" for i in range(150)]
    long_pieces = [t for _, r, t in chunks if "абзац" in t]
    assert len(long_pieces) > 1  # a very long answer becomes several pieces
    assert "абзац0" in long_pieces[0] and "абзац2499" in long_pieces[-1]
    assert {r[0] for r in chunks} == {"antigravity"}
    store.close()


def test_secrets_in_tool_traffic_never_reach_chunks_and_hub_ignores_antigravity(
        tmp_path, monkeypatch):
    home = tmp_path / ".gemini"
    app = home / "antigravity"
    _db(app, [(CONV, json.dumps(["file:///work/alpha"]), "", "", 0)])
    rows = [_user(0, "проверь деплой")]
    rows += _tool(1, "run_command", {"CommandLine": f"curl -H 'x-api-key: {FAKE_KEY}'"},
                  f"ANTHROPIC_API_KEY={FAKE_KEY}\nPASSWORD=hunter2xyzzy")
    rows.append(_reply(3, "Деплой прошёл."))
    _write(app, CONV, rows)

    store = Store(tmp_path / "r.db")
    _index(store, config.antigravity_dirs(home))
    texts = [t for _, _, t in _chunks(store)]
    assert texts == ["проверь деплой", "Деплой прошёл."]
    dump = "\n".join(texts)
    assert FAKE_KEY not in dump and "hunter2xyzzy" not in dump
    store.close()

    # The team hub uploads Claude and Codex files only; Antigravity transcripts
    # (and their raw tool output) stay on this machine.
    monkeypatch.setattr(config, "ANTIGRAVITY_HOME", home)
    pushed = local_files(claude_root=tmp_path / "none-c",
                         codex_sessions=tmp_path / "none-x",
                         codex_archive=tmp_path / "none-a")
    assert pushed == []


def test_moving_canonical_file_leaves_one_copy_of_the_session(tmp_path):
    ide = tmp_path / "antigravity"
    cli = tmp_path / "antigravity-cli"
    roots = (ide, cli)
    # Stage 1: only the compact transcript exists.
    compact = _write(ide, CONV, [_user(0, "первый вопрос"), _reply(1, "первый ответ")],
                     filename="transcript.jsonl")
    store = Store(tmp_path / "r.db")
    _index(store, roots)
    assert [p for (p,) in store.db.execute("SELECT path FROM indexed_files")] == [str(compact)]

    # Stage 2: the full transcript appears next to it — it becomes canonical.
    full = _write(ide, CONV, [_user(0, "первый вопрос"), _reply(1, "первый ответ"),
                              _user(2, "второй вопрос")])
    _index(store, roots)
    assert [p for (p,) in store.db.execute("SELECT path FROM indexed_files")] == [str(full)]
    assert [t for _, _, t in _chunks(store)] == ["первый вопрос", "первый ответ",
                                                "второй вопрос"]

    # Stage 3: the same conversation shows up larger under another surface.
    moved = _write(cli, CONV, [_user(0, "первый вопрос"), _reply(1, "первый ответ"),
                               _user(2, "второй вопрос"), _reply(3, "второй ответ")])
    os.utime(moved, (full.stat().st_mtime + 10,) * 2)
    _index(store, roots)
    assert [p for (p,) in store.db.execute("SELECT path FROM indexed_files")] == [str(moved)]
    texts = [t for _, _, t in _chunks(store)]
    assert texts.count("первый вопрос") == 1 and "второй ответ" in texts
    hits = Recall(store, FakeEmbedder()).recall_search("первый вопрос", source="antigravity")
    assert len({(h.session_id, h.uuid) for h in hits}) == len(hits)
    assert antigravity_transcript_files(roots) == [moved]
    store.close()


def test_partial_roots_do_not_drop_other_surfaces(tmp_path):
    ide = tmp_path / "antigravity"
    cli = tmp_path / "antigravity-cli"
    other = "99999999-9999-4999-8999-999999999999"
    _write(ide, CONV, [_user(0, "из IDE")])
    cli_path = _write(cli, other, [_user(0, "из CLI")])
    store = Store(tmp_path / "r.db")
    _index(store, (ide, cli))
    _index(store, (ide,))  # a caller that scans one surface only
    paths = {p for (p,) in store.db.execute("SELECT path FROM indexed_files")}
    assert str(cli_path) in paths and len(paths) == 2
    store.close()


def test_mixed_index_with_claude_and_codex_filters_by_source(tmp_path):
    claude_root = tmp_path / "claude"
    (claude_root / "-proj").mkdir(parents=True)
    shutil.copy(FIXTURES / "session_a.jsonl", claude_root / "-proj" / "session_a.jsonl")
    codex_root = tmp_path / "codex"
    codex_root.mkdir()
    (codex_root / "rollout.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"timestamp": "2026-07-02T10:00:00Z", "type": "session_meta",
         "payload": {"id": "codex-1", "cwd": "/work/codex", "source": "vscode"}},
        {"timestamp": "2026-07-02T10:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "how do we cache embeddings in codex"}},
        {"timestamp": "2026-07-02T10:00:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "Codex caches embeddings", "phase": "final"}},
    ]))
    agy = tmp_path / "antigravity"
    _db(agy, [(CONV, json.dumps(["file:///work/agy"]), "", "", 0)])
    _write(agy, CONV, [_user(0, "how do we cache embeddings in antigravity"),
                       _reply(1, "Antigravity caches embeddings")])

    store = Store(tmp_path / "r.db")
    _index(store, (agy,), claude=claude_root, codex=(codex_root,))
    assert {s for (s,) in store.db.execute("SELECT DISTINCT source FROM chunks")} == {
        "claude", "codex", "antigravity"}

    recall = Recall(store, FakeEmbedder())
    everything = recall.recall_search("cache embeddings", k=20, per_session=0)
    assert {a.source for a in everything} == {"claude", "codex", "antigravity"}
    for source in ("claude", "codex", "antigravity"):
        only = recall.recall_search("cache embeddings", k=20, source=source)
        assert only and {a.source for a in only} == {source}
        recent = recall.recent_sessions(source=source)
        assert recent and {s["source"] for s in recent} == {source}
    assert {a.source for a in recall.grep("cache", source="antigravity")} == {"antigravity"}
    with pytest.raises(ValueError, match="antigravity"):
        recall.recall_search("x", source="gemini")
    store.close()


def test_harness_link_label_survives_a_truncated_envelope():
    from session_recall.snippets import human_ask
    header = ("Harness Link peer message. The text below is from an agent. "
              "Workspace: /work/app (mode: review, read-only).\n")
    whole = header + json.dumps({"message_id": "m1", "from": "claude:1",
                                 "text": "Обсуди черновик ТЗ\nи собери идеи"})
    assert human_ask(whole) == "Обсуди черновик ТЗ\nи собери идеи"
    # The first chunk piece of a long message: JSON cut mid-string.
    cut = header + '{"message_id":"m2","from":"claude:1","text":"Привет. Я Claude Code,\\nработаю'
    assert human_ask(cut) == "Привет. Я Claude Code,\nработаю"
    # Cut inside an escape sequence.
    cut_escape = header + '{"message_id":"m3","text":"Привет \\u041'
    assert human_ask(cut_escape) == "Привет"
    # No text field at all: keep the envelope rather than lose the turn.
    assert human_ask(header + '{"message_id":"m4"').startswith('{"message_id"')
