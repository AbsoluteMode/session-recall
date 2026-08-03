"""Cursor as the third source. The fixture database reproduces the format
captured from a live Cursor 3.14.7 install (composerHeaders + cursorDiskKV,
bubbles typed 1=user / 2=assistant, thinking as empty-text bubbles) — the
extractor must survive exactly that shape, and reconciliation must track
Cursor's catalog rather than the filesystem."""

import json
import sqlite3
from pathlib import Path

from session_recall import config
from session_recall.cursor import index_cursor, read_sessions
from session_recall.embed import FakeEmbedder
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
        for i, (btype, text, iso) in enumerate(turns):
            bid = f"b{i}"
            headers.append({"bubbleId": bid, "type": btype, "createdAt": iso})
            conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                         (f"bubbleId:{cid}:{bid}",
                          json.dumps({"_v": 3, "type": btype, "bubbleId": bid,
                                      "text": text})))
        conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                     (f"composerData:{cid}",
                      json.dumps({"_v": 1, "composerId": cid, "name": f"chat {cid}",
                                  "fullConversationHeadersOnly": headers})))
    conn.commit()
    conn.close()
    return db


_TURNS = [
    (1, "почему падает деплой по пятницам?", "2026-08-03T10:00:00.000Z"),
    (2, "", "2026-08-03T10:00:01.000Z"),                  # thinking bubble
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


def test_index_cursor_end_to_end_with_workspace_mapping(tmp_path):
    db = _make_db(tmp_path, [("comp-1", "ws-1", 1_700_000_000_000, 0, _TURNS)])
    ws = tmp_path / "User" / "workspaceStorage" / "ws-1"
    ws.mkdir(parents=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": "file:///Users/me/deploy-service"}))

    store = Store(tmp_path / "i.db")
    n = index_cursor(store, FakeEmbedder(), db_path=db)
    assert n == 1
    rows = store.db.execute(
        "SELECT role, project, cwd, source FROM chunks ORDER BY turn_index").fetchall()
    assert rows == [("user", "deploy-service", "/Users/me/deploy-service", "cursor"),
                    ("assistant", "deploy-service", "/Users/me/deploy-service", "cursor")]

    # unchanged catalog → nothing re-indexed
    assert index_cursor(store, FakeEmbedder(), db_path=db) == 0

    # an appended bubble bumps lastUpdatedAt → exactly that session re-indexes
    _make_db(tmp_path, [("comp-1", "ws-1", 1_700_000_999_000, 0,
                         _TURNS + [(1, "а по субботам?", "2026-08-03T11:00:00.000Z")])])
    assert index_cursor(store, FakeEmbedder(), db_path=db) == 1
    assert store.db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 3
    store.close()


def test_reconciliation_follows_the_catalog_not_the_disk(tmp_path):
    db = _make_db(tmp_path, [
        ("comp-1", "empty-window", 1_700_000_000_000, 0, _TURNS),
        ("comp-2", "empty-window", 1_700_000_000_000, 0, _TURNS),
    ])
    store = Store(tmp_path / "i.db")
    assert index_cursor(store, FakeEmbedder(), db_path=db) == 2

    # generic prune must NOT touch virtual cursor paths…
    assert store.prune_deleted() == 0
    # …and a session deleted inside Cursor falls out via reconciliation
    _make_db(tmp_path, [("comp-1", "empty-window", 1_700_000_000_000, 0, _TURNS)])
    index_cursor(store, FakeEmbedder(), db_path=db)
    left = {r[0] for r in store.db.execute(
        "SELECT DISTINCT session_id FROM chunks WHERE source='cursor'")}
    assert left == {"comp-1"}
    store.close()


def test_missing_cursor_install_is_silent(tmp_path):
    store = Store(tmp_path / "i.db")
    assert index_cursor(store, FakeEmbedder(),
                        db_path=tmp_path / "nope" / "state.vscdb") == 0
    store.close()


def test_embedder_swap_invalidates_the_reuse_cache(tmp_path, monkeypatch):
    db = _make_db(tmp_path, [("comp-1", "empty-window", 1_700_000_000_000, 0, _TURNS)])
    store = Store(tmp_path / "i.db")
    emb = FakeEmbedder()
    index_cursor(store, emb, db_path=db)
    first_calls = emb.doc_calls

    # same texts, new fingerprint: the by-hash cache must NOT be reused
    monkeypatch.setattr(config, "EMBED_MODEL", "swapped-model")
    index_cursor(store, emb, db_path=db)
    assert emb.doc_calls > first_calls, \
        "old-space vectors must be re-embedded, never reused across spaces"
    store.close()
