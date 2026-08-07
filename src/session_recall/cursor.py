"""Cursor sessions as a durable third recall source.

Cursor keeps every workspace's conversations in one SQLite database.  The
database is an implementation detail and its schema has changed over time, so
the adapter follows two rules:

1. inspect capabilities instead of assuming one exact set of columns;
2. turn each session into a normalized, content-addressed JSONL snapshot under
   session-recall's data directory.

The snapshot is what chunks point at.  Consequently ``expand_around``, ``step``
and raw ``grep`` use the same streaming machinery as Claude/Codex and continue
working when Cursor is closed, upgraded, or later uninstalled.  Every bubble is
preserved in sanitized raw form, including currently-unknown tool/reasoning
shapes; only non-empty user/assistant surface text is embedded.

The currently-observed schema (Cursor 3.14.7) uses ``composerHeaders`` as the
catalog and ``cursorDiskKV`` keys ``composerData:<id>`` plus
``bubbleId:<composerId>:<bubbleId>``. Tool results and thinking are nested in
assistant bubbles; empty capability/result fields also exist on normal visible
messages, so classification checks payloads rather than key presence. Older
installs without the catalog table fall back to scanning ``composerData:``
keys. SQLite's online backup API makes a transactionally consistent read of a
live WAL database without copying a possibly-mismatched db/WAL pair.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .models import Chunk
from .store import Store
from .transcripts import sanitize_raw

SIG_TAG = "cursor-v2"
LEGACY_VPATH_PREFIX = "cursor:"
SNAPSHOT_DIRNAME = "cursor-transcripts"


class CursorSchemaError(RuntimeError):
    """Cursor exists, but its local store has no shape we can safely read."""


@dataclass
class CursorEvent:
    bubble_id: str
    role: str
    event_type: str
    text: str
    content: str
    ts: int
    raw_header: dict = field(default_factory=dict)
    raw_bubble: dict = field(default_factory=dict)


@dataclass
class CursorSession:
    composer_id: str
    workspace_id: str
    name: str
    updated_ms: int
    # Surface turns are the only records sent to an embedding provider.
    turns: list[dict] = field(default_factory=list)
    # Every bubble, including tool/reasoning/unknown records, reaches raw recall.
    events: list[CursorEvent] = field(default_factory=list)


def _epoch(value: Any, fallback_ms: int = 0) -> int:
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number / 1000 if abs(number) >= 100_000_000_000 else number)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            number = float(raw)
        except ValueError:
            try:
                return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
        else:
            return int(number / 1000 if abs(number) >= 100_000_000_000 else number)
    return int(fallback_ms // 1000)


def _millis(value: Any) -> int:
    seconds = _epoch(value)
    if not seconds:
        return 0
    if isinstance(value, (int, float)) and abs(float(value)) >= 100_000_000_000:
        return int(value)
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            pass
        else:
            if abs(number) >= 100_000_000_000:
                return int(number)
    return seconds * 1000


def _json_dict(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError, UnicodeDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


def _snapshot_database(db_path: Path, tmp: Path) -> Path:
    """Use SQLite backup for one consistent view of a live WAL database."""
    copy = tmp / "cursor-snapshot.db"
    uri = db_path.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=2)
    target = sqlite3.connect(copy)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return copy


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "null"}
    return bool(value)


def _catalog(conn: sqlite3.Connection) -> list[tuple[str, str, int, bool, dict | None]]:
    """(id, workspace, updated_ms, is_subagent, preloaded composer data)."""
    tables = _tables(conn)
    if "cursorDiskKV" not in tables:
        raise CursorSchemaError("Cursor database has no cursorDiskKV table")

    if "composerHeaders" in tables:
        cols = _columns(conn, "composerHeaders")
        if "composerId" in cols:
            workspace = "workspaceId" if "workspaceId" in cols else "''"
            if "lastUpdatedAt" in cols:
                updated = "lastUpdatedAt"
            elif "createdAt" in cols:
                updated = "createdAt"
            else:
                updated = "0"
            subagent = "isSubagent" if "isSubagent" in cols else "0"
            rows = conn.execute(
                f"SELECT composerId, {workspace}, {updated}, {subagent} "
                "FROM composerHeaders"
            ).fetchall()
            return [
                (str(cid), str(ws or ""), _millis(changed), _truthy(sub), None)
                for cid, ws, changed, sub in rows if cid
            ]

    # Older Cursor builds had no separate catalog.  composerData is enough to
    # enumerate sessions; bubble bodies still live under bubbleId keys.
    rows = conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
    ).fetchall()
    catalog: list[tuple[str, str, int, bool, dict | None]] = []
    for key, value in rows:
        data = _json_dict(value)
        if data is None:
            continue
        cid = str(data.get("composerId") or str(key).split(":", 1)[-1])
        workspace = str(data.get("workspaceId") or data.get("workspace") or "")
        changed = (data.get("lastUpdatedAt") or data.get("updatedAt")
                   or data.get("createdAt") or 0)
        subagent = data.get("isSubagent") or data.get("isAgenticSubagent")
        catalog.append((cid, workspace, _millis(changed), _truthy(subagent), data))
    if not catalog and rows:
        raise CursorSchemaError("Cursor composerData rows are not valid JSON objects")
    return catalog


def _text_parts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_text_parts(item))
        return out
    if not isinstance(value, dict):
        return []
    direct = value.get("text")
    if isinstance(direct, str) and direct.strip():
        return [direct]
    out: list[str] = []
    for key in ("content", "message", "summary"):
        if key in value:
            out.extend(_text_parts(value[key]))
    return out


def _has_payload(value: Any) -> bool:
    """Whether a Cursor envelope contains an actual event, not a default.

    Live bubbles carry fields such as ``supportedTools`` and ``toolResults`` on
    every message.  Looking only at key names therefore misclassifies ordinary
    user/assistant text as a tool event.  Payload presence, rather than schema
    capability, is the important distinction.
    """
    if value is None or value is False:
        return False
    if isinstance(value, (str, bytes, bytearray, list, tuple, dict, set)):
        return bool(value)
    return True


def _surface_text(bubble: dict, event_type: str) -> str:
    """Visible conversation only — tool/reasoning payloads never embed."""
    if event_type in {"tool", "reasoning"}:
        return ""
    if "text" in bubble:
        return "\n".join(_text_parts(bubble.get("text"))).strip()
    # Some older stores used content/message for visible bubbles.  Accept that
    # only when no key suggests a tool/function envelope.
    if any(
        _has_payload(value)
        and ("tool" in str(key).casefold() or "function" in str(key).casefold())
        and str(key).casefold() not in {"supportedtools", "availabletools"}
        for key, value in bubble.items()
    ):
        return ""
    return "\n".join(_text_parts(
        bubble.get("content", bubble.get("message"))
    )).strip()


def _role(kind: Any, bubble: dict, header: dict) -> str:
    if kind == 1 or str(kind).casefold() in {"1", "user", "human"}:
        return "user"
    if kind == 2 or str(kind).casefold() in {"2", "assistant", "ai"}:
        return "assistant"
    named = str(bubble.get("role") or header.get("role") or "").casefold()
    if named in {"user", "human"}:
        return "user"
    if named in {"assistant", "ai"}:
        return "assistant"
    if "tool" in named:
        return "tool"
    return ""


def _event_type(role: str, bubble: dict, header: dict) -> str:
    for key in ("bubbleType", "kind", "eventType"):
        value = bubble.get(key, header.get(key))
        if isinstance(value, str) and value:
            return value
    # Numeric ``type`` is the role in current Cursor stores (1=user,
    # 2=assistant).  A non-empty visible text wins even though all live bubbles
    # also contain empty tool capability/result fields.
    visible = "\n".join(_text_parts(bubble.get("text"))).strip()
    if visible and role in {"user", "assistant"}:
        return role
    if any(_has_payload(bubble.get(key))
           for key in ("thinking", "reasoning", "thought", "analysis")):
        return "reasoning"
    if any(
        _has_payload(value)
        and ("tool" in str(key).casefold() or "function" in str(key).casefold())
        and str(key).casefold() not in {"supportedtools", "availabletools"}
        for key, value in bubble.items()
    ):
        return "tool"
    return role or "cursor_event"


def _event_content(text: str, event_type: str, bubble: dict) -> str:
    if text:
        return text
    for key in ("thinking", "reasoning", "thought", "analysis"):
        parts = _text_parts(bubble.get(key))
        if parts:
            return f"[thinking] {' '.join(parts)}"
    safe = sanitize_raw(bubble)
    if isinstance(safe, dict) and set(safe) <= {
            "_v", "type", "bubbleId", "id", "text", "createdAt", "timestamp"}:
        # Cursor currently emits an empty assistant bubble as a thinking
        # placeholder. Preserve it for exact raw grep, but do not make step()
        # land on a record with no readable information.
        return ""
    try:
        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(safe)
    # Raw remains complete in the snapshot; this is only the readable preview
    # returned by expand/step for a shape we do not yet recognize.
    return f"[{event_type}] {rendered[:1200]}" if rendered else ""


def _headers(data: dict) -> list[dict]:
    field = next((key for key in (
        "fullConversationHeadersOnly", "conversation", "bubbles"
    ) if key in data), None)
    if field is None:
        raise CursorSchemaError(
            "Cursor composerData has no supported conversation header field")
    value = data[field]
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        raise CursorSchemaError(
            f"Cursor composerData field {field!r} is not an array/object")
    if any(not isinstance(item, dict) for item in value):
        raise CursorSchemaError(
            f"Cursor composerData field {field!r} contains non-object headers")
    return value


def _inline_bubble(header: dict) -> dict | None:
    for key in ("bubble", "data"):
        inline = _json_dict(header.get(key))
        if inline is not None:
            return inline
    # Legacy stores put the complete bubble directly in ``conversation``.
    keys = {str(key).casefold() for key in header}
    if keys & {
        "text", "content", "message", "thinking", "reasoning", "thought",
        "analysis", "toolresults", "toolresult", "toolcall", "toolcalls",
        "toolname", "functioncall", "functionresult",
    }:
        return dict(header)
    return None


def _sessions_from_connection(conn: sqlite3.Connection):
    for composer_id, workspace_id, updated_ms, is_subagent, loaded in _catalog(conn):
        if is_subagent:
            continue
        data = loaded
        if data is None:
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"composerData:{composer_id}",),
            ).fetchone()
            data = _json_dict(row[0]) if row and row[0] else None
        if data is None:
            raise CursorSchemaError(
                "Cursor catalog references missing or non-JSON composerData")
        sess = CursorSession(
            composer_id=composer_id,
            workspace_id=workspace_id or str(data.get("workspaceId") or ""),
            name=str(data.get("name") or ""),
            updated_ms=updated_ms or _millis(
                data.get("lastUpdatedAt") or data.get("updatedAt") or 0),
        )
        for index, header in enumerate(_headers(data)):
            bubble_id = str(header.get("bubbleId") or header.get("id") or "")
            if not bubble_id:
                bubble_id = f"cursor:{composer_id}:{index}"
            row = conn.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?",
                (f"bubbleId:{composer_id}:{bubble_id}",),
            ).fetchone()
            if row and row[0] is not None:
                bubble = _json_dict(row[0])
                if bubble is None:
                    raise CursorSchemaError(
                        "Cursor bubble row is not a JSON object")
            else:
                bubble = _inline_bubble(header)
                if bubble is None:
                    # A header-only bubble can be observed while Cursor is
                    # writing. Abort this source so reconciliation retains the
                    # prior complete snapshot for retry.
                    raise CursorSchemaError(
                        "Cursor conversation header has no readable bubble body")
            kind = bubble.get("type", header.get("type"))
            role = _role(kind, bubble, header)
            event_type = _event_type(role, bubble, header)
            text = _surface_text(bubble, event_type)
            timestamp = (header.get("createdAt") or bubble.get("createdAt")
                         or header.get("timestamp") or bubble.get("timestamp"))
            ts = _epoch(timestamp, sess.updated_ms)
            event = CursorEvent(
                bubble_id=bubble_id, role=role, event_type=event_type,
                text=text, content=_event_content(text, event_type, bubble), ts=ts,
                raw_header=dict(header), raw_bubble=dict(bubble),
            )
            sess.events.append(event)
            if text and role in {"user", "assistant"}:
                sess.turns.append({
                    "bubble_id": bubble_id, "role": role,
                    "text": text, "ts": ts,
                })
        if sess.events:
            yield sess


def _iter_sessions(db_path: Path):
    """Stream one session at a time from a consistent read-only snapshot."""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = _snapshot_database(Path(db_path), Path(tmp))
            conn = sqlite3.connect(snapshot)
            try:
                yield from _sessions_from_connection(conn)
            finally:
                conn.close()
    except (sqlite3.DatabaseError, OSError) as exc:
        raise CursorSchemaError(f"cannot snapshot/read Cursor database: {exc}") from exc


def read_sessions(db_path: Path) -> list[CursorSession]:
    """Materialize sessions for callers that need the compatibility API."""
    return list(_iter_sessions(Path(db_path)))


def latest_activity(db_path: Path) -> int:
    """Newest conversation-event timestamp in a consistent Cursor snapshot.

    Health uses event time instead of the SQLite file mtime: Cursor also writes
    unrelated settings to the global database, and treating those writes as new
    conversation history would report a false indexing lag.
    """
    newest = 0
    for session in _iter_sessions(Path(db_path)):
        newest = max(newest, *(event.ts for event in session.events))
    return newest


def workspace_folder(db_path: Path, workspace_id: str) -> tuple[str, str]:
    """Resolve Cursor's VS Code-style workspace id to (project, cwd)."""
    if not workspace_id or workspace_id == "empty-window":
        return "", ""
    ws = db_path.parent.parent / "workspaceStorage" / workspace_id / "workspace.json"
    try:
        folder = json.loads(ws.read_text(encoding="utf-8")).get("folder") or ""
    except (OSError, ValueError):
        return "", ""
    if folder.startswith("file://"):
        path = unquote(urlparse(folder).path)
        return Path(path).name, path
    return "", ""


def _embed_fp() -> str:
    from . import config
    return config.embed_fingerprint()


def _snapshot_bytes(session: CursorSession, project: str, cwd: str) -> tuple[bytes, dict[str, tuple[int, int, int]]]:
    payload = bytearray()
    positions: dict[str, tuple[int, int, int]] = {}
    for index, event in enumerate(session.events):
        obj = {
            "type": event.event_type,
            "uuid": event.bubble_id,
            "sessionId": session.composer_id,
            "timestamp": event.ts,
            "cwd": cwd,
            "project": project,
            "message": {"role": event.role, "content": event.content},
            "cursor": {
                "sessionName": session.name,
                "workspaceId": session.workspace_id,
                "header": sanitize_raw(event.raw_header),
                "bubble": sanitize_raw(event.raw_bubble),
            },
        }
        line = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        positions[event.bubble_id] = (len(payload), len(line), index)
        payload.extend(line)
    return bytes(payload), positions


def _snapshot_path(snapshot_dir: Path, composer_id: str, digest: str) -> Path:
    opaque = hashlib.sha256(composer_id.encode()).hexdigest()[:24]
    return snapshot_dir / f"{opaque}-{digest[:16]}.jsonl"


def _write_snapshot(path: Path, payload: bytes) -> bool:
    """Atomic and idempotent; returns whether this call created the file."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _prior_paths(store: Store, session_id: str) -> list[str]:
    rows = store.db.execute(
        "SELECT DISTINCT file_path FROM chunks WHERE source = 'cursor' AND session_id = ?",
        (session_id,),
    ).fetchall()
    legacy = f"{LEGACY_VPATH_PREFIX}{session_id}"
    paths = [str(row[0]) for row in rows]
    if store.stored_sig(legacy) and legacy not in paths:
        paths.append(legacy)
    return paths


def index_cursor(store: Store, embedder, db_path: Path | None = None,
                 snapshot_dir: Path | None = None) -> int:
    """Index Cursor and materialize navigable raw snapshots.

    Absence is silent and retains earlier snapshots.  An incompatible/corrupt
    database raises :class:`CursorSchemaError`; the CLI boundary reports that
    source as skipped without undoing successful Claude/Codex work.
    """
    from . import config as app_config

    db_path = Path(db_path or app_config.CURSOR_DB)
    snapshot_dir = Path(snapshot_dir or (app_config.DATA_DIR / SNAPSHOT_DIRNAME))
    if not db_path.exists():
        return 0

    count = 0
    seen_paths: set[str] = set()
    # Stream from the SQLite backup so a large Cursor history does not keep
    # every raw bubble in memory at once.
    for sess in _iter_sessions(db_path):
        project, cwd = workspace_folder(db_path, sess.workspace_id)
        payload, positions = _snapshot_bytes(sess, project, cwd)
        digest = hashlib.sha256(payload).hexdigest()
        snapshot = _snapshot_path(snapshot_dir, sess.composer_id, digest)
        snapshot_s = str(snapshot)
        seen_paths.add(snapshot_s)
        sig = f"{SIG_TAG}:{_embed_fp()}:{sess.updated_ms}:{len(sess.events)}:{digest}"
        if store.is_indexed(snapshot_s, sig) and snapshot.exists():
            continue

        prior = _prior_paths(store, sess.composer_id)
        cached: dict[str, bytes] = {}
        for old_path in prior:
            old_sig = store.stored_sig(old_path) or ""
            if f":{_embed_fp()}:" in old_sig:
                cached.update(store.embeddings_by_hash(old_path))

        created = _write_snapshot(snapshot, payload)
        old_files = [Path(path) for path in prior
                     if path != snapshot_s and not path.startswith(LEGACY_VPATH_PREFIX)]
        try:
            chunks: list[Chunk] = []
            for turn in sess.turns:
                offset, length, turn_index = positions[turn["bubble_id"]]
                chunks.append(Chunk(
                    session_id=sess.composer_id, uuid=turn["bubble_id"],
                    role=turn["role"], text=turn["text"], project=project, cwd=cwd,
                    git_branch="", ts=turn["ts"], file_path=snapshot_s,
                    byte_offset=offset, byte_len=length, turn_index=turn_index,
                    content_hash=hashlib.sha256(turn["text"].encode()).hexdigest(),
                    source="cursor",
                ))

            missing: dict[str, str] = {}
            for chunk in chunks:
                if chunk.content_hash not in cached:
                    missing.setdefault(chunk.content_hash, chunk.text)
            texts = list(missing.values())
            vectors = embedder.embed_documents(texts) if texts else []
            if len(vectors) != len(texts):
                raise RuntimeError(
                    f"embedder returned {len(vectors)} vectors for {len(texts)} texts")
            fresh = dict(zip(missing, vectors))

            # Replace all older snapshots for this composer in one DB
            # transaction.  The content-addressed files themselves are removed
            # only after commit, so rollback always leaves navigable old rows.
            for old_path in prior:
                if old_path == snapshot_s:
                    continue
                store.delete_file(old_path)
                store.db.execute("DELETE FROM indexed_files WHERE path = ?", (old_path,))
            store.delete_file(snapshot_s)
            for chunk in chunks:
                vector = cached.get(chunk.content_hash)
                store.add(chunk, vector if vector is not None else fresh[chunk.content_hash])
            store.mark_indexed(snapshot_s, sig, source="cursor")
            store.refresh_embed_meta(_embed_fp())
            store.commit()
            count += 1
        except Exception:
            store.rollback()
            if created:
                snapshot.unlink(missing_ok=True)
            raise
        for old_file in old_files:
            old_file.unlink(missing_ok=True)

    # Sessions deleted inside Cursor fall out of the index and snapshot store.
    stale = [str(row[0]) for row in store.db.execute(
        "SELECT path FROM indexed_files WHERE source = 'cursor'").fetchall()
        if str(row[0]) not in seen_paths]
    stale_files = [Path(path) for path in stale
                   if not path.startswith(LEGACY_VPATH_PREFIX)]
    for path in stale:
        store.delete_file(path)
        store.db.execute("DELETE FROM indexed_files WHERE path = ?", (path,))
    store.refresh_embed_meta(_embed_fp())
    store.commit()
    for path in stale_files:
        path.unlink(missing_ok=True)
    return count
