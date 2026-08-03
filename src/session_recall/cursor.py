"""Cursor (cursor.com) sessions as a third index source.

Format captured from a LIVE Cursor 3.14.7 install (2026-08-03), not from
docs: one SQLite database `<UserData>/User/globalStorage/state.vscdb` holds
every session across every workspace:

- table `composerHeaders(composerId, workspaceId, createdAt, lastUpdatedAt,
  isArchived, isSubagent, …)` — the session catalog;
- `cursorDiskKV` row `composerData:<id>` — a JSON header whose
  `fullConversationHeadersOnly` lists bubbles in order, each with an ISO
  timestamp;
- `cursorDiskKV` rows `bubbleId:<composerId>:<bubbleId>` — the messages:
  `type` 1 = user, 2 = assistant. Thinking arrives as empty-text assistant
  bubbles, so the surface rule stays the project invariant: non-empty text
  only, tool noise never reaches the index.

Unlike Claude/Codex there is no file per session, so incremental indexing
keys on virtual paths `cursor:<composerId>` with `lastUpdatedAt` baked into
the signature, and reconciliation compares the catalog against
`indexed_files` instead of the filesystem (`prune_deleted` skips the
virtual prefix). The live database is snapshotted before reading: Cursor
keeps it open in WAL mode, and a copy is the one read that can never block
the editor or tear mid-transaction.

Subagent sessions (`isSubagent=1`) are skipped like Claude sidechains and
Codex spawned agents. The workspace→folder mapping follows the VS Code
convention (`workspaceStorage/<id>/workspace.json`, a `folder` file URI);
sessions run without a folder ("empty-window") index with an empty project.
"""

import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from .models import Chunk
from .store import Store

SIG_TAG = "cursor-v1"
VPATH_PREFIX = "cursor:"          # virtual indexed_files path, never on disk


@dataclass
class CursorSession:
    composer_id: str
    workspace_id: str
    name: str
    updated_ms: int
    turns: list = field(default_factory=list)   # {bubble_id, role, text, ts}


def _iso_to_epoch(iso: str, fallback_ms: int) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return fallback_ms // 1000


def _snapshot(db_path: Path, tmp: Path) -> Path:
    """Copy db (+WAL sidecars when present) so the read never races Cursor."""
    copy = tmp / db_path.name
    shutil.copy(db_path, copy)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            shutil.copy(side, tmp / side.name)
    return copy


def read_sessions(db_path: Path) -> list[CursorSession]:
    """The whole catalog, surface turns only, oldest bubble first."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = sqlite3.connect(_snapshot(db_path, Path(tmp)))
        try:
            headers = conn.execute(
                "SELECT composerId, workspaceId, lastUpdatedAt, COALESCE(isSubagent, 0) "
                "FROM composerHeaders").fetchall()
            out: list[CursorSession] = []
            for composer_id, workspace_id, updated_ms, is_subagent in headers:
                if is_subagent:
                    continue
                raw = conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ?",
                    (f"composerData:{composer_id}",)).fetchone()
                if not raw or not raw[0]:
                    continue
                try:
                    data = json.loads(raw[0])
                except ValueError:
                    continue
                sess = CursorSession(
                    composer_id=composer_id, workspace_id=workspace_id or "",
                    name=data.get("name") or "", updated_ms=int(updated_ms or 0))
                for h in data.get("fullConversationHeadersOnly") or []:
                    bubble_id = h.get("bubbleId")
                    if not bubble_id:
                        continue
                    brow = conn.execute(
                        "SELECT value FROM cursorDiskKV WHERE key = ?",
                        (f"bubbleId:{composer_id}:{bubble_id}",)).fetchone()
                    if not brow or not brow[0]:
                        continue
                    try:
                        bubble = json.loads(brow[0])
                    except ValueError:
                        continue
                    text = (bubble.get("text") or "").strip()
                    btype = bubble.get("type") or h.get("type")
                    if not text or btype not in (1, 2):
                        continue     # thinking/tool bubbles carry no surface text
                    sess.turns.append({
                        "bubble_id": bubble_id,
                        "role": "user" if btype == 1 else "assistant",
                        "text": text,
                        "ts": _iso_to_epoch(h.get("createdAt", ""), sess.updated_ms),
                    })
                if sess.turns:
                    out.append(sess)
            return out
        finally:
            conn.close()


def workspace_folder(db_path: Path, workspace_id: str) -> tuple[str, str]:
    """(project, cwd) via the VS Code workspace.json convention; sessions
    without a folder — Cursor's "empty-window" — get an empty project."""
    if not workspace_id or workspace_id == "empty-window":
        return "", ""
    ws = db_path.parent.parent / "workspaceStorage" / workspace_id / "workspace.json"
    try:
        folder = json.loads(ws.read_text()).get("folder") or ""
    except (OSError, ValueError):
        return "", ""
    if folder.startswith("file://"):
        path = unquote(urlparse(folder).path)
        return Path(path).name, path
    return "", ""


def _embed_fp() -> str:
    from . import config
    return config.embed_fingerprint()


def index_cursor(store: Store, embedder, db_path: Path | None = None) -> int:
    """Index changed Cursor sessions; returns how many were (re)indexed.
    A machine without Cursor is silent — absence is not an error."""
    from . import config as app_config
    db_path = db_path or app_config.CURSOR_DB
    if not Path(db_path).exists():
        return 0
    sessions = read_sessions(Path(db_path))
    count = 0
    seen_vpaths = set()
    for sess in sessions:
        vpath = f"{VPATH_PREFIX}{sess.composer_id}"
        seen_vpaths.add(vpath)
        sig = f"{SIG_TAG}:{_embed_fp()}:{sess.updated_ms}:{len(sess.turns)}"
        if store.is_indexed(vpath, sig):
            continue
        project, cwd = workspace_folder(Path(db_path), sess.workspace_id)
        # vector reuse is only sound within one embedding space: an fp change
        # invalidates the signature AND must invalidate the by-hash cache
        old_sig = store.stored_sig(vpath) or ""
        cached = (store.embeddings_by_hash(vpath)
                  if f":{_embed_fp()}:" in old_sig else {})
        try:
            chunks, vecs = [], []
            for i, t in enumerate(sess.turns):
                chunk = Chunk(
                    session_id=sess.composer_id, uuid=t["bubble_id"],
                    role=t["role"], text=t["text"], project=project, cwd=cwd,
                    git_branch="", ts=t["ts"], file_path=vpath,
                    byte_offset=0, byte_len=len(t["text"].encode()),
                    turn_index=i,
                    content_hash=hashlib.sha256(t["text"].encode()).hexdigest(),
                    source="cursor")
                chunks.append(chunk)
            missing = [c.text for c in chunks if c.content_hash not in cached]
            fresh_vecs = embedder.embed_documents(missing) if missing else []
            fresh = dict(zip((c.content_hash for c in chunks
                              if c.content_hash not in cached), fresh_vecs))
            store.delete_file(vpath)
            for chunk in chunks:
                vec = cached.get(chunk.content_hash)
                store.add(chunk, vec if vec is not None else fresh[chunk.content_hash])
            store.mark_indexed(vpath, sig, source="cursor")
            store.commit()
            count += 1
        except Exception:
            store.rollback()
            raise
    # reconciliation replaces prune: the "files" live in Cursor's catalog,
    # not on disk, so compare against what the catalog still contains
    stale = [r[0] for r in store.db.execute(
        "SELECT path FROM indexed_files WHERE source = 'cursor'").fetchall()
        if r[0] not in seen_vpaths]
    for vpath in stale:
        store.delete_file(vpath)
        store.db.execute("DELETE FROM indexed_files WHERE path = ?", (vpath,))
    if stale:
        store.commit()
    return count
