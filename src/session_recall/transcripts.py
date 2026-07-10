"""Streaming adapters for Claude Code and Codex JSONL transcripts.

The envelopes are unrelated, so indexing and retrieval consume normalized
events from here. Files are always streamed: Codex rollouts can be hundreds of
megabytes and grep may touch the entire corpus.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterator

from .models import Chunk
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


def clean_codex_user_text(content: str) -> str | None:
    cleaned = _CODEX_HARNESS_BLOCK.sub("", content).strip()
    return cleaned or None


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
            # An ordinary Claude turn proves this is not a Codex rollout and
            # avoids scanning a whole Claude transcript looking for metadata.
            if obj.get("type") in {"user", "assistant"} and "message" in obj:
                return None
    return None


def detect_source(path: str | Path) -> str:
    return "codex" if _first_session_meta(path) else "claude"


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


def extractor_version(source: str) -> str:
    versions = {"claude": "2", "codex": "1"}
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


def _render_codex(obj: dict) -> str:
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


def iter_transcript_events(path: str | Path, source: str | None = None) -> Iterator[TranscriptEvent]:
    """Yield normalized records with byte-stable cursors, one JSONL line at a time."""
    path = str(path)
    source = source or detect_source(path)
    fallback_sid = _codex_id_from_path(path) if source == "codex" else ""
    session_id = fallback_sid
    cwd = ""
    git_branch = ""
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
                if key not in {"encrypted_content", "signature"}}
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
    if source != "codex":
        raise ValueError(f"unknown transcript source: {source!r}")

    events = list(iter_transcript_events(path, source="codex"))
    turns: list[dict[str, Any]] = []
    for event in events:
        obj = event.obj
        payload = obj.get("payload") or {}
        payload = payload if isinstance(payload, dict) else {}
        kind = payload.get("type")
        envelope = obj.get("type")
        if envelope == "event_msg" and kind in {"user_message", "agent_message"}:
            role = "user" if kind == "user_message" else "assistant"
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
        if event.obj.get("type") == "event_msg" and kind in {
                "user_message", "agent_message"}:
            if not event.content.strip():
                continue
            role = "user" if kind == "user_message" else "assistant"
            chunks.append(make_chunk(event, role, event.content))
    return chunks


def is_navigable(event: TranscriptEvent) -> bool:
    # Preserve Claude's historic raw-line stepping. Codex bookkeeping records
    # are skipped so navigation lands on readable messages/tools/reasoning.
    if event.source == "claude":
        return True
    payload = event.obj.get("payload") or {}
    if (event.obj.get("type") == "response_item" and isinstance(payload, dict)
            and payload.get("type") == "message"):
        return False  # mirrored by the canonical event_msg surface
    return bool(event.content)
