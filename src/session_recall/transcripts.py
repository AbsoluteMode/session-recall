"""Streaming adapters for Claude Code, Codex, Antigravity and normalized Cursor JSONL.

The envelopes are unrelated, so indexing and retrieval consume normalized
events from here. Files are always streamed: Codex rollouts can be hundreds of
megabytes and grep may touch the entire corpus.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from .models import Chunk
from .chunking import surface_pieces, surface_role
from .scope import project_label


_CODEX_ID = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)
_CODEX_HARNESS_TAGS = (
    "environment_context", "recommended_plugins", "skill", "turn_aborted",
    "subagent_notification", "image", "user_action",
)
_CODEX_HARNESS_BLOCK = re.compile(
    "|".join(rf"<{tag}>.*?</{tag}>" for tag in _CODEX_HARNESS_TAGS), re.DOTALL,
)

_ANTIGRAVITY_UUID = re.compile(
    r"/brain/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/"
)
_ANTIGRAVITY_HARNESS_TAGS = (
    "ADDITIONAL_METADATA", "USER_SETTINGS_CHANGE", "SYSTEM_MESSAGE",
    "EPHEMERAL_MESSAGE", "CONTEXT_SUMMARY",
)
_ANTIGRAVITY_HARNESS_BLOCK = re.compile(
    "|".join(rf"<{tag}\b[^>]*>.*?</{tag}>" for tag in _ANTIGRAVITY_HARNESS_TAGS),
    re.DOTALL,
)
_ANTIGRAVITY_USER_OPEN = re.compile(r"(?m)^[ \t]*<USER_REQUEST>[ \t]*\r?\n?")
_ANTIGRAVITY_USER_CLOSE = re.compile(r"(?m)\r?\n?^[ \t]*</USER_REQUEST>[ \t]*$")
_HARNESS_LINK_WORKSPACE = re.compile(r"\bWorkspace:\s*(/[^\s(]+)")

# db_path -> (mtime_ns, {conversation_id: (cwd, is_sidechain)})
_ANTIGRAVITY_DB_CACHE: dict[str, tuple[int, dict[str, tuple[str, bool]]]] = {}


@dataclass(frozen=True)
class TranscriptFileMeta:
    source: str
    session_id: str
    cwd: str
    git_branch: str
    is_sidechain: bool = False

    def __getitem__(self, key: str):
        # Small mapping compatibility for callers/tests that predate the typed
        # metadata object.
        if key == "is_subagent":
            return self.is_sidechain
        return getattr(self, key)


@dataclass(frozen=True)
class TranscriptSpec:
    path: Path
    project: str
    source: str


@dataclass(frozen=True)
class TranscriptEvent:
    source: str
    obj: dict
    session_id: str
    uuid: str
    cwd: str
    git_branch: str
    ts: int
    timestamp: str
    role: str
    type: str
    content: str
    byte_offset: int
    byte_len: int
    turn_index: int


def parse_ts(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _codex_id_from_path(path: str | Path) -> str:
    match = _CODEX_ID.search(str(path))
    return match.group(1) if match else Path(path).stem


def _codex_uuid_from_path(path: str | Path) -> str | None:
    match = _CODEX_ID.search(str(path))
    return match.group(1) if match else None


def codex_cursor(session_id: str, byte_offset: int) -> str:
    """Stable opaque cursor: rollouts are append-only and may move to archive."""
    return f"codex:{session_id}:{byte_offset}"


def antigravity_cursor(session_id: str, byte_offset: int) -> str:
    """Stable opaque cursor into an Antigravity conversation transcript."""
    return f"antigravity:{session_id}:{byte_offset}"


def clean_codex_user_text(content: str) -> str | None:
    cleaned = _CODEX_HARNESS_BLOCK.sub("", content).strip()
    return cleaned or None


def clean_antigravity_user_text(content: str) -> str | None:
    """Extract only the user's own prompt from an Antigravity USER_INPUT step."""
    if not isinstance(content, str) or not content:
        return None
    cleaned = _ANTIGRAVITY_HARNESS_BLOCK.sub("", content).strip()
    cleaned = _ANTIGRAVITY_USER_OPEN.sub("", cleaned)
    cleaned = _ANTIGRAVITY_USER_CLOSE.sub("", cleaned)
    cleaned = cleaned.strip()
    if cleaned.startswith("<USER_REQUEST>") and cleaned.endswith("</USER_REQUEST>"):
        cleaned = cleaned[len("<USER_REQUEST>"):-len("</USER_REQUEST>")].strip()
    return cleaned or None


def _antigravity_id_and_app_dir(path: str | Path) -> tuple[str, Path | None]:
    p = Path(path)
    if (len(p.parents) >= 4 and p.parent.name == "logs"
            and p.parents[1].name == ".system_generated"
            and p.parents[3].name == "brain"):
        return p.parents[2].name, p.parents[4]
    match = _ANTIGRAVITY_UUID.search(p.as_posix())
    if match:
        return match.group(1), None
    return p.stem, None


def _workspace_from_uris(raw_uris: Any) -> str:
    if not raw_uris:
        return ""
    uris = raw_uris
    if isinstance(raw_uris, str):
        try:
            uris = json.loads(raw_uris)
        except ValueError:
            return ""
    if not isinstance(uris, list):
        return ""
    for uri in uris:
        if not isinstance(uri, str) or not uri:
            continue
        if uri.startswith("file://"):
            parsed = unquote(urlparse(uri).path)
            if parsed.startswith("/"):
                return parsed
        elif uri.startswith("/"):
            return uri
    return ""


def _antigravity_db_summary(app_dir: Path | None,
                            session_id: str) -> tuple[str, bool] | None:
    if app_dir is None:
        return None
    db_path = Path(app_dir) / "conversation_summaries.db"
    try:
        st = db_path.stat()
    except OSError:
        return None
    key = str(db_path)
    cached = _ANTIGRAVITY_DB_CACHE.get(key)
    if cached is None or cached[0] != st.st_mtime_ns:
        mapping: dict[str, tuple[str, bool]] = {}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT conversation_id, workspace_uris, agent_name, "
                    "parent_conversation_id, nesting_depth "
                    "FROM conversation_summaries"
                ).fetchall()
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            return None
        for conv_id, raw_uris, agent_name, parent_id, depth in rows:
            if not conv_id:
                continue
            cwd = _workspace_from_uris(raw_uris)
            try:
                depth_int = int(depth or 0)
            except (TypeError, ValueError):
                depth_int = 0
            sidechain = bool(
                str(parent_id or "").strip()
                or str(agent_name or "").strip()
                or depth_int > 0
            )
            mapping[str(conv_id)] = (cwd, sidechain)
        _ANTIGRAVITY_DB_CACHE[key] = (st.st_mtime_ns, mapping)
        cached = _ANTIGRAVITY_DB_CACHE[key]
    return cached[1].get(session_id)


def antigravity_file_header(path: str | Path) -> TranscriptFileMeta:
    """Resolve Antigravity session identity, workspace cwd and subagent status."""
    session_id, app_dir = _antigravity_id_and_app_dir(path)
    summary = _antigravity_db_summary(app_dir, session_id)
    cwd = summary[0] if summary is not None else ""
    is_sidechain = summary[1] if summary is not None else False

    if summary is None or not cwd:
        saw_leading_system = False
        saw_user_explicit = False
        checked_first_record = False
        try:
            with open(path, "rb") as handle:
                for raw in handle:
                    try:
                        obj = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(obj, dict):
                        continue
                    if not checked_first_record:
                        checked_first_record = True
                        if (obj.get("source") == "SYSTEM"
                                and obj.get("type") == "SYSTEM_MESSAGE"):
                            saw_leading_system = True
                    if (obj.get("source") == "USER_EXPLICIT"
                            and obj.get("type") == "USER_INPUT"):
                        saw_user_explicit = True
                        if not cwd:
                            cleaned = clean_antigravity_user_text(obj.get("content") or "") or ""
                            if cleaned.startswith("Harness Link peer message."):
                                found = _HARNESS_LINK_WORKSPACE.search(cleaned)
                                if found:
                                    cwd = found.group(1)
                        break
                    if summary is not None and cwd:
                        break
        except OSError:
            pass
        if summary is None and saw_leading_system and not saw_user_explicit:
            is_sidechain = True

    return TranscriptFileMeta(
        source="antigravity",
        session_id=session_id,
        cwd=cwd,
        git_branch="",
        is_sidechain=is_sidechain,
    )


def antigravity_transcript_files(roots: Iterable[Path]) -> list[Path]:
    """Canonical transcript path per Antigravity conversation across app surfaces."""
    picked: dict[str, tuple[tuple[int, int, int], Path]] = {}
    for root in roots:
        brain = Path(root) / "brain"
        if not brain.is_dir():
            continue
        for conv_dir in sorted(brain.iterdir()):
            if not conv_dir.is_dir():
                continue
            logs = conv_dir / ".system_generated" / "logs"
            candidate = logs / "transcript_full.jsonl"
            if not candidate.is_file():
                candidate = logs / "transcript.jsonl"
                if not candidate.is_file():
                    continue
            try:
                st = candidate.stat()
            except OSError:
                continue
            rank = (1 if candidate.name == "transcript_full.jsonl" else 0,
                    st.st_size, st.st_mtime_ns)
            prev = picked.get(conv_dir.name)
            if prev is None or rank >= prev[0]:
                picked[conv_dir.name] = (rank, candidate)
    return [item[1] for _, item in sorted(picked.items())]


def _source_is_subagent(value: Any) -> bool:
    """Recognize string and nested ``source.subagent`` metadata shapes."""
    if isinstance(value, str):
        return value.casefold() == "subagent"
    if isinstance(value, dict):
        if "subagent" in value:
            return True
        return any(_source_is_subagent(item) for item in value.values())
    if isinstance(value, list):
        return any(_source_is_subagent(item) for item in value)
    return False


def _first_session_meta(path: str | Path) -> dict | None:
    """Find Codex metadata even if a future writer adds a JSON preamble."""
    with open(path, "rb") as handle:
        for raw in handle:
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                return obj
            # An ordinary Claude or Antigravity turn proves this is not a Codex
            # rollout and avoids scanning a whole transcript looking for metadata.
            if obj.get("type") in {"user", "assistant"} and "message" in obj:
                return None
            if "step_index" in obj and obj.get("source") in {"USER_EXPLICIT", "MODEL", "SYSTEM"}:
                return None
    return None


def detect_source(path: str | Path) -> str:
    with open(path, "rb") as handle:
        for raw in handle:
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                return "codex"
            if "step_index" in obj and obj.get("source") in {"USER_EXPLICIT", "MODEL", "SYSTEM"}:
                return "antigravity"
            if obj.get("type") in {"user", "assistant"} and "message" in obj:
                return "claude"
    return "claude"


def codex_file_header(path: str | Path) -> TranscriptFileMeta:
    """Read only the first Codex metadata record; no conversation is buffered."""
    fallback_id = _codex_id_from_path(path)
    first = _first_session_meta(path) or {}
    payload = first.get("payload") if first.get("type") == "session_meta" else {}
    payload = payload if isinstance(payload, dict) else {}
    source = payload.get("source")
    is_sidechain = bool(
        _source_is_subagent(payload.get("thread_source"))
        or payload.get("agent_path")
        or _source_is_subagent(source)
    )
    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    # id names THIS rollout. session_id can identify the parent in sidechains.
    session_id = str(
        payload.get("id") or _codex_uuid_from_path(path)
        or payload.get("session_id") or fallback_id
    )
    return TranscriptFileMeta(
        source="codex", session_id=session_id, cwd=str(payload.get("cwd") or ""),
        git_branch=str(git.get("branch") or ""), is_sidechain=is_sidechain,
    )


def codex_file_meta(path: str | Path) -> TranscriptFileMeta:
    """Return stable identity plus the latest repeated session metadata.

    Discovery/indexing uses :func:`codex_file_header` to avoid an extra full
    scan. This fuller public helper is useful for diagnostics and metadata-only
    callers that want resumed cwd/git values.
    """
    header = codex_file_header(path)
    cwd = header.cwd
    branch = header.git_branch
    sidechain = header.is_sidechain
    for event in iter_transcript_events(path, source="codex"):
        if event.obj.get("type") != "session_meta":
            continue
        payload = event.obj.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        cwd = str(payload.get("cwd") or cwd)
        git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
        branch = str(git.get("branch") or branch)
        source = payload.get("source")
        sidechain = bool(sidechain or _source_is_subagent(payload.get("thread_source"))
                         or payload.get("agent_path")
                         or _source_is_subagent(source))
    return TranscriptFileMeta("codex", header.session_id, cwd, branch, sidechain)


def discover_codex_transcripts(sessions_dir: Path, archived_dir: Path) -> list[TranscriptSpec]:
    candidates: set[Path] = set()
    for root in (Path(sessions_dir), Path(archived_dir)):
        if root.is_dir():
            candidates.update(path for path in root.rglob("*.jsonl") if path.is_file())
    specs: list[TranscriptSpec] = []
    for path in sorted(candidates, key=str):
        meta = codex_file_meta(path)
        if not meta.is_sidechain:
            specs.append(TranscriptSpec(path, project_label(meta.cwd), "codex"))
    return specs


def discover_antigravity_transcripts(roots: Iterable[Path]) -> list[TranscriptSpec]:
    specs: list[TranscriptSpec] = []
    for path in antigravity_transcript_files(roots):
        meta = antigravity_file_header(path)
        if not meta.is_sidechain:
            specs.append(TranscriptSpec(path, project_label(meta.cwd) or "antigravity", "antigravity"))
    return specs


def extractor_version(source: str) -> str:
    versions = {"claude": "3", "codex": "3", "cursor": "2", "antigravity": "1"}
    try:
        return versions[source]
    except KeyError as exc:
        raise ValueError(f"unknown transcript source: {source!r}") from exc


def _json_preview(value, limit: int) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = str(value)
    return rendered[:limit]


def _text_parts(value) -> list[str]:
    """Flatten public text fields, never ciphertext or signatures."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_text_parts(item))
        return out
    if not isinstance(value, dict):
        return []
    text = value.get("text")
    if isinstance(text, str) and text.strip():
        return [text]
    out: list[str] = []
    for key in ("content", "summary"):
        if key in value:
            out.extend(_text_parts(value[key]))
    return out


def _render_claude(obj: dict) -> str:
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "thinking":
            thinking = (block.get("thinking") or "").strip()
            if thinking:
                parts.append(f"[thinking] {thinking}")
        elif kind == "tool_use":
            parts.append(
                f"[tool_use:{block.get('name', '')}] "
                f"{_json_preview(block.get('input', {}), 300)}"
            )
        elif kind == "tool_result":
            parts.append(f"[tool_result] {_json_preview(block.get('content'), 500)}")
    return "\n".join(part for part in parts if part)


def _completed_message(obj: dict) -> tuple[str, str] | None:
    """Codex desktop records visible messages as completed typed items.

    Only explicit UserMessage/AgentMessage items are conversation surface;
    tool output, reasoning and mirrored response_item messages stay excluded.
    """
    payload = obj.get("payload")
    if (obj.get("type") != "event_msg" or not isinstance(payload, dict)
            or payload.get("type") != "item_completed"):
        return None
    item = payload.get("item")
    if not isinstance(item, dict):
        return None
    role = {"UserMessage": "user", "AgentMessage": "assistant"}.get(item.get("type"))
    if role is None:
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    text = "\n".join(block["text"] for block in content
                     if isinstance(block, dict)
                     and block.get("type") in {"text", "Text", "input_text", "output_text"}
                     and isinstance(block.get("text"), str) and block["text"].strip())
    return role, text


def _render_codex(obj: dict) -> str:
    completed = _completed_message(obj)
    if completed is not None:
        return completed[1]
    envelope = obj.get("type")
    payload = obj.get("payload") or {}
    if not isinstance(payload, dict):
        return ""
    kind = payload.get("type", "")
    if envelope == "event_msg":
        if kind in {"user_message", "agent_message"}:
            message = payload.get("message")
            if not isinstance(message, str):
                return ""
            return message
        if kind == "agent_reasoning":
            text = payload.get("text") or payload.get("message")
            return f"[thinking] {text}" if isinstance(text, str) and text.strip() else ""
        if kind == "exec_command_end":
            output = (payload.get("formatted_output") or payload.get("aggregated_output")
                      or payload.get("stdout") or payload.get("stderr"))
            return f"[tool_result:exec] {_json_preview(output, 500)}" if output else ""
        if kind == "patch_apply_end":
            value = payload.get("changes") or payload.get("stdout") or payload.get("stderr")
            return f"[tool_result:patch] {_json_preview(value, 500)}" if value else ""
        if kind == "mcp_tool_call_end":
            value = payload.get("result")
            return f"[tool_result:mcp] {_json_preview(value, 500)}" if value else ""
        if kind == "error":
            message = payload.get("message")
            return f"[error] {message}" if isinstance(message, str) else ""
        return ""
    if envelope != "response_item":
        return ""
    if kind == "message":
        role = payload.get("role", "")
        if role not in {"user", "assistant"}:
            return ""
        wanted = "input_text" if role == "user" else "output_text"
        content = payload.get("content") or []
        parts = [
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == wanted
            and isinstance(block.get("text"), str)
        ]
        text = "\n".join(part for part in parts if part.strip())
        return (clean_codex_user_text(text) or "") if role == "user" else text
    if kind == "reasoning":
        parts = _text_parts(payload.get("summary") or payload.get("content"))
        return f"[thinking] {' '.join(parts)}" if parts else ""
    if kind in {"function_call", "custom_tool_call", "web_search_call", "tool_search_call"}:
        name = payload.get("name") or kind
        args = payload.get("arguments", payload.get("input", payload.get("action", {})))
        return f"[tool_use:{name}] {_json_preview(args, 300)}"
    if kind in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
        return f"[tool_result] {_json_preview(payload.get('output'), 500)}"
    if kind == "agent_message":
        parts = _text_parts(payload.get("content"))
        return f"[agent_message] {' '.join(parts)}" if parts else ""
    return ""


def _codex_role_and_type(obj: dict) -> tuple[str, str]:
    completed = _completed_message(obj)
    if completed is not None:
        return completed[0], "item_completed"
    envelope = obj.get("type", "")
    payload = obj.get("payload") or {}
    payload = payload if isinstance(payload, dict) else {}
    kind = str(payload.get("type") or envelope)
    if envelope == "response_item" and kind == "message":
        return str(payload.get("role") or ""), kind
    if kind == "user_message":
        return "user", kind
    if kind in {"agent_message", "agent_reasoning", "reasoning"}:
        return "assistant", kind
    if "call" in kind or kind.endswith("_end") or kind.endswith("_output"):
        return "tool", kind
    return "", kind


def _antigravity_role_and_type(obj: dict) -> tuple[str, str]:
    step_source = str(obj.get("source") or "")
    step_type = str(obj.get("type") or "")
    if step_source == "USER_EXPLICIT" and step_type == "USER_INPUT":
        return "user", "user_input"
    if step_source == "MODEL" and step_type == "PLANNER_RESPONSE":
        raw_content = obj.get("content")
        if isinstance(raw_content, str) and raw_content.strip():
            return "assistant", "planner_response"
        if isinstance(obj.get("tool_calls"), list) and obj.get("tool_calls"):
            return "tool", "planner_response"
        return "assistant", "planner_response"
    if step_source == "MODEL" and step_type == "GENERIC":
        return "tool", "tool_result"
    return "", step_type.lower() or "system"


def _render_antigravity(obj: dict) -> str:
    step_source = obj.get("source")
    step_type = obj.get("type")
    if step_source == "USER_EXPLICIT" and step_type == "USER_INPUT":
        return clean_antigravity_user_text(obj.get("content") or "") or ""
    if step_source == "MODEL" and step_type == "PLANNER_RESPONSE":
        raw_content = obj.get("content")
        visible = raw_content.strip() if isinstance(raw_content, str) else ""
        tool_calls = obj.get("tool_calls") if isinstance(obj.get("tool_calls"), list) else []
        if visible and not tool_calls:
            return visible
        if tool_calls:
            parts = [visible] if visible else []
            for call in tool_calls:
                if isinstance(call, dict):
                    parts.append(
                        f"[tool_use:{call.get('name', '')}] "
                        f"{_json_preview(call.get('args', {}), 300)}"
                    )
            return "\n".join(part for part in parts if part)
        return ""
    if step_source == "MODEL" and step_type == "GENERIC":
        out = obj.get("content")
        return f"[tool_result] {_json_preview(out, 500)}" if isinstance(out, str) and out.strip() else ""
    return ""


def iter_transcript_events(path: str | Path, source: str | None = None) -> Iterator[TranscriptEvent]:
    """Yield normalized records with byte-stable cursors, one JSONL line at a time."""
    path = str(path)
    source = source or detect_source(path)
    fallback_sid = _codex_id_from_path(path) if source == "codex" else ""
    session_id = fallback_sid
    cwd = ""
    git_branch = ""
    if source == "antigravity":
        agy_meta = antigravity_file_header(path)
        session_id = agy_meta.session_id
        cwd = agy_meta.cwd
        git_branch = agy_meta.git_branch
    saw_meta = False
    offset = 0
    with open(path, "rb") as handle:
        for turn_index, raw in enumerate(handle):
            byte_offset = offset
            byte_len = len(raw)
            offset += byte_len
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            if source == "claude":
                message = obj.get("message") or {}
                timestamp = str(obj.get("timestamp") or "")
                sid = str(obj.get("sessionId") or "")
                yield TranscriptEvent(
                    source=source, obj=obj, session_id=sid,
                    uuid=str(obj.get("uuid") or f"claude:{sid}:{byte_offset}"),
                    cwd=str(obj.get("cwd") or ""), git_branch=str(obj.get("gitBranch") or ""),
                    ts=parse_ts(timestamp), timestamp=timestamp,
                    role=str(message.get("role") or obj.get("type") or ""),
                    type=str(obj.get("type") or ""), content=_render_claude(obj),
                    byte_offset=byte_offset, byte_len=byte_len, turn_index=turn_index,
                )
                continue

            if source == "cursor":
                message = obj.get("message") or {}
                message = message if isinstance(message, dict) else {}
                content = message.get("content")
                if not isinstance(content, str):
                    content = _json_preview(content, 1200) if content is not None else ""
                timestamp_value = obj.get("timestamp")
                timestamp = str(timestamp_value or "")
                sid = str(obj.get("sessionId") or fallback_sid)
                yield TranscriptEvent(
                    source=source, obj=obj, session_id=sid,
                    uuid=str(obj.get("uuid") or f"cursor:{sid}:{byte_offset}"),
                    cwd=str(obj.get("cwd") or ""), git_branch="",
                    ts=parse_ts(timestamp_value), timestamp=timestamp,
                    role=str(message.get("role") or ""),
                    type=str(obj.get("type") or "cursor_event"), content=content,
                    byte_offset=byte_offset, byte_len=byte_len, turn_index=turn_index,
                )
                continue

            if source == "antigravity":
                timestamp = str(obj.get("created_at") or obj.get("timestamp") or "")
                role, event_type = _antigravity_role_and_type(obj)
                yield TranscriptEvent(
                    source=source, obj=obj, session_id=session_id,
                    uuid=antigravity_cursor(session_id, byte_offset),
                    cwd=cwd, git_branch=git_branch,
                    ts=parse_ts(timestamp), timestamp=timestamp,
                    role=role, type=event_type, content=_render_antigravity(obj),
                    byte_offset=byte_offset, byte_len=byte_len, turn_index=turn_index,
                )
                continue

            payload = obj.get("payload") or {}
            payload = payload if isinstance(payload, dict) else {}
            if obj.get("type") == "session_meta":
                meta_id = str(
                    payload.get("id") or _codex_uuid_from_path(path)
                    or payload.get("session_id") or fallback_sid
                )
                if not saw_meta:
                    saw_meta = True
                    session_id = meta_id
                # Identity remains the first meta id; repeated resume metadata
                # may refresh cwd/git for following records.
                cwd = str(payload.get("cwd") or cwd)
                git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
                git_branch = str(git.get("branch") or git_branch)
            elif obj.get("type") == "turn_context":
                cwd = str(payload.get("cwd") or cwd)
            timestamp = str(obj.get("timestamp") or payload.get("timestamp") or "")
            role, event_type = _codex_role_and_type(obj)
            yield TranscriptEvent(
                source=source, obj=obj, session_id=session_id,
                uuid=codex_cursor(session_id, byte_offset), cwd=cwd, git_branch=git_branch,
                ts=parse_ts(timestamp), timestamp=timestamp, role=role, type=event_type,
                content=_render_codex(obj), byte_offset=byte_offset, byte_len=byte_len,
                turn_index=turn_index,
            )


def sanitize_raw(value: Any) -> Any:
    """Recursively remove opaque reasoning ciphertext/signatures from output."""
    if isinstance(value, dict):
        return {key: sanitize_raw(item) for key, item in value.items()
                if str(key).casefold() not in {
                    "encrypted_content", "encryptedcontent", "signature"}}
    if isinstance(value, list):
        return [sanitize_raw(item) for item in value]
    return value


def _claude_shaped(event: TranscriptEvent, *, surface: bool,
                   role: str = "", event_type: str | None = None,
                   content: Any = None) -> dict[str, Any]:
    payload = event.obj.get("payload") or {}
    payload = payload if isinstance(payload, dict) else {}
    turn = {
        "uuid": event.uuid,
        "sessionId": event.session_id,
        "cwd": event.cwd,
        "timestamp": event.timestamp,
        "type": event_type or event.type,
        "message": {"role": role or event.role, "content": content if content is not None else []},
        "__raw": sanitize_raw(event.obj),
        "__source": event.source,
        "__surface": surface,
        "__byte_offset": event.byte_offset,
        "__byte_len": event.byte_len,
        "__turn_index": event.turn_index,
        "__git_branch": event.git_branch,
    }
    if payload.get("phase") is not None:
        turn["phase"] = payload["phase"]
    return turn


def read_transcript(path: str, source: str) -> list[dict[str, Any]]:
    """Compatibility materializer; production retrieval uses the streaming iterator."""
    if source == "claude":
        return [event.obj for event in iter_transcript_events(path, source="claude")]
    if source == "cursor":
        return [event.obj for event in iter_transcript_events(path, source="cursor")]
    if source == "antigravity":
        turns: list[dict[str, Any]] = []
        for event in iter_transcript_events(path, source="antigravity"):
            obj = event.obj
            if obj.get("source") == "USER_EXPLICIT" and obj.get("type") == "USER_INPUT":
                text = clean_antigravity_user_text(obj.get("content") or "") or ""
                turns.append(_claude_shaped(
                    event, surface=bool(text), role="user", event_type="user", content=text))
            elif obj.get("source") == "MODEL" and obj.get("type") == "PLANNER_RESPONSE":
                raw = obj.get("content")
                text = raw.strip() if isinstance(raw, str) else ""
                content = [{"type": "text", "text": text}] if text else []
                turns.append(_claude_shaped(
                    event, surface=bool(text), role="assistant",
                    event_type="assistant", content=content))
            else:
                turns.append(_claude_shaped(event, surface=False, content=[]))
        return turns
    if source != "codex":
        raise ValueError(f"unknown transcript source: {source!r}")

    events = list(iter_transcript_events(path, source="codex"))
    turns = []
    for event in events:
        obj = event.obj
        payload = obj.get("payload") or {}
        payload = payload if isinstance(payload, dict) else {}
        kind = payload.get("type")
        envelope = obj.get("type")
        if envelope == "event_msg" and (kind in {"user_message", "agent_message"}
                                        or _completed_message(obj) is not None):
            role = event.role
            content: Any = event.content if role == "user" else [{"type": "text", "text": event.content}]
            turns.append(_claude_shaped(
                event, surface=bool(event.content), role=role, event_type=role, content=content))
            continue
        if envelope == "response_item" and kind == "message":
            role = str(payload.get("role") or "")
            content = (event.content if role == "user"
                       else ([{"type": "text", "text": event.content}] if event.content else []))
            turns.append(_claude_shaped(
                event, surface=False, role=role, event_type=role or "message", content=content))
            continue
        if envelope == "response_item" and kind == "reasoning":
            parts = _text_parts(payload.get("summary") or payload.get("content"))
            content = ([{"type": "thinking", "thinking": " ".join(parts)}] if parts else [])
            turns.append(_claude_shaped(
                event, surface=False, role="assistant", event_type="assistant", content=content))
            continue
        if envelope == "response_item" and kind in {
                "function_call", "custom_tool_call", "web_search_call", "tool_search_call"}:
            args = payload.get("arguments", payload.get("input", payload.get("action", {})))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"arguments": args}
            block = {"type": "tool_use", "name": payload.get("name") or kind,
                     "input": sanitize_raw(args)}
            turns.append(_claude_shaped(
                event, surface=False, role="assistant", event_type="assistant", content=[block]))
            continue
        if envelope == "response_item" and kind in {
                "function_call_output", "custom_tool_call_output", "tool_search_output"}:
            block = {"type": "tool_result", "content": sanitize_raw(payload.get("output"))}
            turns.append(_claude_shaped(
                event, surface=False, role="tool", event_type="tool", content=[block]))
            continue
        turns.append(_claude_shaped(event, surface=False, content=[]))
    return turns


def extract_codex_file(path: str, project: str = "") -> list[Chunk]:
    """Stream only the canonical visible Codex event surface.

    Do not call :func:`read_transcript` here: that compatibility helper
    materializes every raw record, while real rollouts can be hundreds of MB.
    response_item user records are intentionally excluded: in real legacy and
    service rollouts they can contain machine instructions that look like user
    text. They remain available to explicit grep/expand.
    """
    chunks: list[Chunk] = []

    def make_chunk(event: TranscriptEvent, role: str, text: str) -> Chunk:
        return Chunk(
            session_id=event.session_id,
            uuid=event.uuid,
            role=role,
            text=text,
            project=project_label(event.cwd) or project,
            cwd=event.cwd,
            git_branch=event.git_branch,
            ts=event.ts,
            file_path=path,
            byte_offset=event.byte_offset,
            byte_len=event.byte_len,
            turn_index=event.turn_index,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source="codex",
        )

    for event in iter_transcript_events(path, source="codex"):
        payload = event.obj.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if event.obj.get("type") == "event_msg" and (kind in {
                "user_message", "agent_message"} or _completed_message(event.obj) is not None):
            if not event.content.strip():
                continue
            role = surface_role(event.role, event.content)
            for piece in surface_pieces(role, event.content):
                chunks.append(make_chunk(event, role, piece))
    return chunks


def extract_antigravity_file(path: str, project: str = "") -> list[Chunk]:
    """Stream only the clean user prompts and visible assistant replies.

    Thinking, tool calls, tool execution outputs (GENERIC), system messages and
    compaction checkpoints are never embedded into chunks.
    """
    chunks: list[Chunk] = []

    def make_chunk(event: TranscriptEvent, role: str, text: str) -> Chunk:
        return Chunk(
            session_id=event.session_id,
            uuid=event.uuid,
            role=role,
            text=text,
            project=project_label(event.cwd) or project,
            cwd=event.cwd,
            git_branch=event.git_branch,
            ts=event.ts,
            file_path=path,
            byte_offset=event.byte_offset,
            byte_len=event.byte_len,
            turn_index=event.turn_index,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source="antigravity",
        )

    for event in iter_transcript_events(path, source="antigravity"):
        obj = event.obj
        step_source = obj.get("source")
        step_type = obj.get("type")
        if step_source == "USER_EXPLICIT" and step_type == "USER_INPUT":
            text = clean_antigravity_user_text(obj.get("content") or "")
            if not text:
                continue
            role = surface_role("user", text)
            for piece in surface_pieces(role, text):
                chunks.append(make_chunk(event, role, piece))
        elif step_source == "MODEL" and step_type == "PLANNER_RESPONSE":
            raw = obj.get("content")
            text = raw.strip() if isinstance(raw, str) else ""
            if not text:
                continue
            role = surface_role("assistant", text)
            for piece in surface_pieces(role, text):
                chunks.append(make_chunk(event, role, piece))
    return chunks


def is_navigable(event: TranscriptEvent) -> bool:
    # Preserve Claude's historic raw-line stepping. Codex/Antigravity bookkeeping
    # records are skipped so navigation lands on readable messages/tools.
    if event.source == "claude":
        return True
    if event.source in ("cursor", "antigravity"):
        return bool(event.content)
    payload = event.obj.get("payload") or {}
    if (event.obj.get("type") == "response_item" and isinstance(payload, dict)
            and payload.get("type") == "message"):
        return False  # mirrored by the canonical event_msg surface
    return bool(event.content)
