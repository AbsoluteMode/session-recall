"""Cursor as the third source. The fixture database reproduces the format
captured from a live Cursor 3.14.7 install (composerHeaders + cursorDiskKV,
bubbles typed 1=user / 2=assistant, thinking as empty-text bubbles) — the
extractor must survive exactly that shape, and reconciliation must track
Cursor's catalog rather than the filesystem."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from session_recall import config
from session_recall.cursor import CursorSchemaError, index_cursor, read_sessions
from session_recall.embed import FakeEmbedder
from session_recall.retrieve import Recall
from session_recall.store import Store


def _make_db(root: Path, sessions) -> Path:
    """sessions: list of (composer_id, workspace_id, updated_ms, is_subagent,
    turns) where turns = [(type, text, iso_ts)]."""
    gs = root / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    db = gs / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS composerHeaders("
                 "composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, "
                 "lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, "
                 "recency INTEGER, checkpointAt INTEGER, value TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV("
                 "key TEXT PRIMARY KEY, value BLOB)")
    conn.execute("DELETE FROM composerHeaders")
    conn.execute("DELETE FROM cursorDiskKV")
    for cid, ws, updated, sub, turns in sessions:
        conn.execute("INSERT INTO composerHeaders VALUES (?,?,?,?,0,?,0,0,'{}')",
                     (cid, ws, updated - 1000, updated, sub))
        headers = []
        for i, turn in enumerate(turns):
            btype, text, iso, *extra = turn
            bid = f"b{i}"
            headers.append({"bubbleId": bid, "type": btype, "createdAt": iso})
            # Current Cursor writes capability/result fields on every bubble,
            # even when no tool was invoked.  They must not hide visible text.
            bubble = {
                "_v": 3, "type": btype, "bubbleId": bid, "text": text,
                "supportedTools": [], "toolResults": [],
            }
            if extra:
                bubble.update(extra[0])
            conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                         (f"bubbleId:{cid}:{bid}",
                          json.dumps(bubble)))
        conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                     (f"composerData:{cid}",
                      json.dumps({"_v": 1, "composerId": cid, "name": f"chat {cid}",
                                  "fullConversationHeadersOnly": headers})))
    conn.commit()
    conn.close()
    return db


_TURNS = [
    (1, "почему падает деплой по пятницам?", "2026-08-03T10:00:00.000Z"),
    (2, "", "2026-08-03T10:00:01.000Z", {
        "thinking": {
            "text": "Проверяю расписание крона.",
            "signature": "cursor-thinking-signature-must-not-escape",
        },
    }),
    (2, "Крон собирал кэш в полночь UTC — по пятницам он пересекался с релизом.",
     "2026-08-03T10:00:02.000Z"),
]


def test_read_sessions_surface_only(tmp_path):
    db = _make_db(tmp_path, [
        ("comp-1", "ws-1", 1_700_000_000_000, 0, _TURNS),
        ("comp-sub", "ws-1", 1_700_000_000_000, 1, _TURNS),   # subagent: skipped
    ])
    (tmp_path / "User" / "workspaceStorage" / "ws-1").mkdir(parents=True)
    sessions = read_sessions(db)
    assert [s.composer_id for s in sessions] == ["comp-1"]
    s = sessions[0]
    assert [t["role"] for t in s.turns] == ["user", "assistant"], \
        "the empty thinking bubble must not reach the surface"
    assert s.turns[0]["text"].startswith("почему падает")
    assert s.turns[0]["ts"] == 1785751200  # 2026-08-03T10:00:00Z
    assert len(s.events) == 3, "raw recall keeps the empty thinking bubble too"
    assert [event.event_type for event in s.events] == [
        "user", "reasoning", "assistant"]


def test_index_cursor_end_to_end_with_workspace_mapping(tmp_path):
    db = _make_db(tmp_path, [("comp-1", "ws-1", 1_700_000_000_000, 0, _TURNS)])
    ws = tmp_path / "User" / "workspaceStorage" / "ws-1"
    ws.mkdir(parents=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": "file:///Users/me/deploy-service"}))

    store = Store(tmp_path / "i.db")
    snapshots = tmp_path / "snapshots"
    n = index_cursor(store, FakeEmbedder(), db_path=db, snapshot_dir=snapshots)
    assert n == 1
    rows = store.db.execute(
        "SELECT role, project, cwd, source FROM chunks ORDER BY turn_index").fetchall()
    assert rows == [("user", "deploy-service", "/Users/me/deploy-service", "cursor"),
                    ("assistant", "deploy-service", "/Users/me/deploy-service", "cursor")]
    # snapshots are written as utf-8 bytes; reading them back in the locale
    # codepage (the Windows default) turns Cyrillic into mojibake
    raw = next(snapshots.glob("*.jsonl")).read_text(encoding="utf-8")
    assert "Проверяю расписание крона" in raw
    assert "cursor-thinking-signature-must-not-escape" not in raw

    # unchanged catalog → nothing re-indexed
    assert index_cursor(
        store, FakeEmbedder(), db_path=db, snapshot_dir=snapshots) == 0

    # an appended bubble bumps lastUpdatedAt → exactly that session re-indexes
    _make_db(tmp_path, [("comp-1", "ws-1", 1_700_000_999_000, 0,
                         _TURNS + [(1, "а по субботам?", "2026-08-03T11:00:00.000Z")])])
    assert index_cursor(
        store, FakeEmbedder(), db_path=db, snapshot_dir=snapshots) == 1
    assert store.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 3
    store.close()


def test_reconciliation_follows_the_catalog_not_the_disk(tmp_path):
    db = _make_db(tmp_path, [
        ("comp-1", "empty-window", 1_700_000_000_000, 0, _TURNS),
        ("comp-2", "empty-window", 1_700_000_000_000, 0, _TURNS),
    ])
    store = Store(tmp_path / "i.db")
    snapshots = tmp_path / "snapshots"
    assert index_cursor(
        store, FakeEmbedder(), db_path=db, snapshot_dir=snapshots) == 2

    # generic prune must NOT touch virtual cursor paths…
    assert store.prune_deleted() == 0
    # …and a session deleted inside Cursor falls out via reconciliation
    _make_db(tmp_path, [("comp-1", "empty-window", 1_700_000_000_000, 0, _TURNS)])
    index_cursor(store, FakeEmbedder(), db_path=db, snapshot_dir=snapshots)
    left = {r[0] for r in store.db.execute(
        "SELECT DISTINCT session_id FROM chunks WHERE source='cursor'")}
    assert left == {"comp-1"}
    assert len(list(snapshots.glob("*.jsonl"))) == 1, \
        "the deleted Cursor session's durable snapshot must be reconciled too"
    store.close()


def test_missing_cursor_install_is_silent(tmp_path):
    store = Store(tmp_path / "i.db")
    assert index_cursor(store, FakeEmbedder(),
                        db_path=tmp_path / "nope" / "state.vscdb",
                        snapshot_dir=tmp_path / "snapshots") == 0
    store.close()


def test_embedder_swap_invalidates_the_reuse_cache(tmp_path, monkeypatch):
    db = _make_db(tmp_path, [("comp-1", "empty-window", 1_700_000_000_000, 0, _TURNS)])
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    snapshots = tmp_path / "snapshots"
    index_cursor(store, emb, db_path=db, snapshot_dir=snapshots)
    first_calls = emb.doc_calls

    # same texts, new fingerprint: the by-hash cache must NOT be reused
    monkeypatch.setattr(config, "EMBED_MODEL", "swapped-model")
    index_cursor(store, emb, db_path=db, snapshot_dir=snapshots)
    assert emb.doc_calls > first_calls, \
        "old-space vectors must be re-embedded, never reused across spaces"
    store.close()


def test_cursor_deep_recall_expand_step_and_raw_grep(tmp_path):
    """Cursor is a first-class source: a semantic anchor can be expanded and
    stepped through, while grep reaches a non-surface tool bubble."""
    turns = [
        (1, "добавь функцию сложения", "2026-08-03T10:00:00Z"),
        (2, "", "2026-08-03T10:00:01Z", {
            # Shape from Cursor 3.14.7's installed ConversationMessage.ToolResult
            # schema: tool actions/results are nested in the assistant bubble.
            "toolResults": [{
                "toolCallId": "call-1",
                "toolName": "write_file",
                "args": '{"path":"calculator.py"}',
                "content": "def add(a,b): return a+b",
                "startedAtMs": 1_785_751_201_000,
                "completedAtMs": 1_785_751_201_100,
            }],
        }),
        (2, "Готово, тесты проходят.", "2026-08-03T10:00:02Z"),
    ]
    db = _make_db(tmp_path, [("comp-tools", "empty-window", 1_700_000_000_000, 0,
                              turns)])
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    snapshots = tmp_path / "snapshots"
    index_cursor(store, emb, db_path=db, snapshot_dir=snapshots)
    recall = Recall(store, emb)

    anchor = recall.recall_search(
        "добавь функцию сложения", source="cursor", k=5)[0]
    window = recall.expand_around(
        anchor.session_id, anchor.uuid, before=0, after=2, source="cursor")
    assert [turn.type for turn in window] == ["user", "tool", "assistant"]
    assert "calculator.py" in window[1].content
    assert recall.step(
        anchor.session_id, anchor.uuid, "next", source="cursor")[0].type == "tool"

    exact = recall.grep("calculator.py", source="cursor")
    assert len(exact) == 1 and exact[0].session_id == "comp-tools"
    assert recall.expand_around(
        exact[0].session_id, exact[0].uuid, source="cursor")
    store.close()


def test_cursor_catalog_falls_back_to_composer_data_keys(tmp_path):
    """Older stores without composerHeaders remain readable via inline data."""
    gs = tmp_path / "User" / "globalStorage"
    gs.mkdir(parents=True)
    db = gs / "state.vscdb"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY, value BLOB)")
    data = {
        "composerId": "legacy-1", "workspaceId": "empty-window",
        "updatedAt": 1_785_751_200_000,
        "conversation": [
            {"bubbleId": "old-u", "type": 1, "createdAt": 1_785_751_200_000,
             "text": "legacy question"},
            {"bubbleId": "old-a", "type": 2, "createdAt": 1_785_751_201_000,
             "text": "legacy answer"},
        ],
    }
    conn.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                 ("composerData:legacy-1", json.dumps(data)))
    conn.commit()
    conn.close()

    sessions = read_sessions(db)
    assert [session.composer_id for session in sessions] == ["legacy-1"]
    assert [turn["text"] for turn in sessions[0].turns] == [
        "legacy question", "legacy answer"]


def test_cursor_unknown_schema_is_explicit(tmp_path):
    db = tmp_path / "state.vscdb"
    sqlite3.connect(db).close()
    with pytest.raises(CursorSchemaError, match="cursorDiskKV"):
        read_sessions(db)


def test_cursor_schema_drift_preserves_last_good_snapshot(tmp_path):
    """Unsupported private-schema changes must fail closed: never reconcile a
    merely unreadable catalog as though the user deleted every conversation."""
    db = _make_db(tmp_path, [("comp-1", "empty-window", 1_700_000_000_000, 0,
                              _TURNS)])
    store = Store(tmp_path / "i.db")
    snapshots = tmp_path / "snapshots"
    index_cursor(store, FakeEmbedder(), db_path=db, snapshot_dir=snapshots)
    before_rows = store.db.execute(
        "SELECT session_id, uuid, file_path FROM chunks ORDER BY turn_index"
    ).fetchall()
    before_files = {path: path.read_bytes() for path in snapshots.glob("*.jsonl")}

    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE cursorDiskKV SET value=? WHERE key='composerData:comp-1'",
        (json.dumps({"composerId": "comp-1", "messagesV99": []}),),
    )
    conn.commit()
    conn.close()

    with pytest.raises(CursorSchemaError, match="conversation header"):
        index_cursor(store, FakeEmbedder(), db_path=db, snapshot_dir=snapshots)
    assert store.db.execute(
        "SELECT session_id, uuid, file_path FROM chunks ORDER BY turn_index"
    ).fetchall() == before_rows
    assert {path: path.read_bytes() for path in snapshots.glob("*.jsonl")} == before_files
    store.close()


def test_cursor_migrates_legacy_virtual_rows_without_reembedding(tmp_path):
    db = _make_db(tmp_path, [("comp-1", "empty-window", 1_700_000_000_000, 0,
                              _TURNS)])
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    # Reproduce the v1 virtual-path representation.
    from session_recall.models import Chunk
    text = _TURNS[0][1]
    legacy = Chunk(
        session_id="comp-1", uuid="b0", role="user", text=text,
        project="", cwd="", git_branch="", ts=1,
        file_path="cursor:comp-1", byte_offset=0, byte_len=len(text),
        turn_index=0, content_hash=hashlib.sha256(text.encode()).hexdigest(),
        source="cursor")
    store.add(legacy, emb.embed_query(text))
    store.mark_indexed(
        "cursor:comp-1",
        f"cursor-v1:{config.embed_fingerprint()}:1700000000000:2",
        source="cursor")
    store.commit()
    calls = emb.doc_calls

    index_cursor(store, emb, db_path=db, snapshot_dir=tmp_path / "snapshots")
    assert store.stored_sig("cursor:comp-1") is None
    assert all(not row[0].startswith("cursor:") for row in store.db.execute(
        "SELECT path FROM indexed_files WHERE source='cursor'"))
    assert emb.doc_calls == calls + 1, \
        "only the previously absent assistant vector should be embedded"
    store.close()
