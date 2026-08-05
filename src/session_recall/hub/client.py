"""The member side: enroll once, then keep the hub current.

Two commands are the whole surface. `join` stores the URL and the key the
operator handed over; `push` walks the local transcript roots and uploads what
the hub does not have yet. Everything else — embedding, indexing, answering —
happens on the server, which is the point: a member installs, joins, and never
thinks about it again.

Uploads are incremental by construction. Transcripts only ever grow, so the
hub's manifest of "bytes consumed per file" is enough to compute the tail to
send; a re-push after a day of work moves kilobytes, not gigabytes. A file
that has SHRUNK locally was rewritten rather than appended to, and is re-sent
whole — the only way for the two sides to converge again.

Redaction happens here, before anything leaves the machine, and it is
deliberately the weaker of the two secret defences: it matches formats, so it
catches an API key and cannot catch a password. The hub's Doppler map catches
what the team actually stores. Doing the format pass client-side means those
findings never travel at all.
"""

import base64
import gzip
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..share.scanner import redact
from . import storage

CONFIG_PATH = config.DATA_DIR / "hub.json"
# Sized from the first real deploy, not from taste. At 256 KB a 3.5 GB
# history moved at ~0.1 MB/s: every chunk is its own request, and urllib
# opens a fresh connection each time, so the cost was one TLS handshake plus
# one fsync per 256 KB rather than the bytes themselves. 4 MB cuts that by
# 16x and still leaves room under the hub's 16 MB body cap once base64
# expands it by a third (the body is gzipped on top, so the wire figure is
# lower again).
CHUNK_BYTES = 4 * 1024 * 1024
_TIMEOUT_S = 300          # a 4 MB chunk on a slow uplink outlasts 60s

CONSENT = """\
Подключение к общему индексу команды

Что произойдёт: транскрипты твоих сессий Claude Code и Codex — включая
промпты, ответы, вызовы инструментов и их вывод — будут загружаться на
сервер команды и станут доступны для поиска ВСЕМ участникам, а не только
тебе.

Что защищено: перед отправкой вырезаются ключи и токены известных форматов,
а на сервере дополнительно маскируются все значения, которые команда хранит
в Doppler. Это тревожная растяжка, а не гарантия — личное в рабочие сессии
лучше не писать.

Отключиться можно в любой момент: `session-recall hub leave`. Уже загруженное
при этом остаётся на сервере — попроси оператора удалить.
"""


class HubError(RuntimeError):
    pass


@dataclass
class HubConfig:
    url: str
    key: str
    consented: int = 0

    @classmethod
    def load(cls, path: Path | None = None) -> "HubConfig | None":
        try:
            data = json.loads(Path(path or CONFIG_PATH).read_text())
        except (OSError, ValueError):
            return None
        if not data.get("url") or not data.get("key"):
            return None
        return cls(data["url"].rstrip("/"), data["key"],
                   int(data.get("consented", 0)))

    def save(self, path: Path | None = None) -> None:
        path = Path(path or CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 0600 before the key is written, not after: a world-readable moment
        # is all it takes on a shared machine.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"url": self.url, "key": self.key,
                       "consented": self.consented}, fh, indent=2)


class HubClient:
    def __init__(self, cfg: HubConfig, timeout: int = _TIMEOUT_S):
        self.cfg = cfg
        self.timeout = timeout

    def _post(self, path: str, body: dict) -> dict:
        raw = gzip.compress(json.dumps(body).encode())
        request = urllib.request.Request(
            self.cfg.url + path, data=raw,
            headers={"Content-Type": "application/json",
                     "Content-Encoding": "gzip",
                     "Authorization": f"Bearer {self.cfg.key}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as failure:
            detail = ""
            try:
                detail = json.loads(failure.read()).get("error", "")
            except Exception:                              # noqa: BLE001
                pass
            if failure.code == 401:
                raise HubError(
                    "hub rejected the key — it may have been revoked; "
                    "ask the operator for a new one") from failure
            if failure.code == 409:
                raise _Conflict() from failure
            raise HubError(f"hub error {failure.code}: {detail}") from failure
        except urllib.error.URLError as failure:
            raise HubError(f"hub unreachable: {failure.reason}") from failure

    def manifest(self) -> dict[str, int]:
        return self._post("/v1/ingest/manifest", {}).get("files", {})

    def send(self, rel: str, offset: int, data: bytes, raw_len: int) -> int:
        return self._post("/v1/ingest", {
            "path": rel, "offset": offset, "raw_len": raw_len,
            "data": base64.b64encode(data).decode()})["size"]


class _Conflict(Exception):
    """The hub holds a different length than we assumed — resend from zero."""


def local_files(claude_root: Path | None = None,
                codex_sessions: Path | None = None,
                codex_archive: Path | None = None) -> list[tuple[str, Path]]:
    """(relative path on the hub, local file) for everything uploadable.

    Cursor is absent by design: its history lives in a SQLite store, not in
    files, so there is nothing to ship byte-for-byte. It stays local until the
    hub speaks that format too.
    """
    claude_root = config.CLAUDE_PROJECTS if claude_root is None else claude_root
    codex_sessions = config.CODEX_SESSIONS if codex_sessions is None else codex_sessions
    codex_archive = (config.CODEX_ARCHIVED_SESSIONS if codex_archive is None
                     else codex_archive)

    found: list[tuple[str, Path]] = []
    if Path(claude_root).is_dir():
        for project in sorted(Path(claude_root).iterdir()):
            if not project.is_dir():
                continue
            # Non-recursive, matching the indexer: nested files are subagent
            # sidechains, which the extractor does not treat as sessions.
            for jsonl in sorted(project.glob("*.jsonl")):
                found.append((f"claude/{project.name}/{jsonl.name}", jsonl))
    for root, prefix in ((codex_sessions, "codex"),
                         (codex_archive, "codex/archived")):
        if not Path(root).is_dir():
            continue
        for jsonl in sorted(Path(root).rglob("*.jsonl")):
            rel = jsonl.relative_to(root).as_posix()
            found.append((f"{prefix}/{rel}", jsonl))
    return found


def push(cfg: HubConfig, progress=None, roots: dict | None = None) -> dict:
    """Bring the hub up to date with this machine. Returns a summary.

    One file failing does not abort the run: a member's history is thousands
    of independent transcripts, and stopping at the first unreadable one would
    mean the whole push never completes.
    """
    progress = progress or (lambda _msg: None)
    client = HubClient(cfg)
    have = client.manifest()
    files = local_files(**(roots or {}))

    stats = {"files": 0, "uploaded_bytes": 0, "redacted": 0,
             "skipped": 0, "failed": 0}
    for rel, path in files:
        if not storage.is_safe_rel(rel):
            # A name the hub would reject. Warn rather than fail: it is one
            # project directory, not the push.
            progress(f"skip (unsupported name): {rel}")
            stats["skipped"] += 1
            continue
        try:
            stats["uploaded_bytes"] += _push_one(
                client, rel, path, have.get(rel, 0), stats, progress)
        except HubError:
            raise                    # auth/network: the whole run is doomed
        except Exception as failure:                       # noqa: BLE001
            progress(f"failed: {rel}: {type(failure).__name__}: {failure}")
            stats["failed"] += 1
    return stats


def _push_one(client: HubClient, rel: str, path: Path, offset: int,
              stats: dict, progress) -> int:
    size = path.stat().st_size
    if size < offset:
        # Locally shorter than what the hub consumed: the transcript was
        # rewritten, not appended to. Resend from the top.
        offset = 0
    if size == offset:
        return 0
    sent_total = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        cursor = offset
        while True:
            piece = fh.read(CHUNK_BYTES)
            if not piece:
                break
            text = piece.decode("utf-8", errors="surrogateescape")
            clean, hits = redact(text)
            stats["redacted"] += hits
            payload = clean.encode("utf-8", errors="surrogateescape")
            try:
                client.send(rel, cursor, payload, raw_len=len(piece))
            except _Conflict:
                # Someone else's copy of this member's history got there
                # first, or a previous run died mid-chunk. Restart the file.
                progress(f"resync: {rel}")
                return _resend_whole(client, rel, path, stats, progress)
            cursor += len(piece)
            sent_total += len(piece)
    stats["files"] += 1
    progress(f"sent {rel} (+{sent_total} B)")
    return sent_total


def _resend_whole(client: HubClient, rel: str, path: Path, stats: dict,
                  progress) -> int:
    sent_total = 0
    with open(path, "rb") as fh:
        cursor = 0
        while True:
            piece = fh.read(CHUNK_BYTES)
            if not piece:
                break
            clean, hits = redact(piece.decode("utf-8", errors="surrogateescape"))
            stats["redacted"] += hits
            client.send(rel, cursor, clean.encode("utf-8", errors="surrogateescape"),
                        raw_len=len(piece))
            cursor += len(piece)
            sent_total += len(piece)
    stats["files"] += 1
    progress(f"resent {rel} ({sent_total} B)")
    return sent_total


def join(url: str, key: str, path: Path | None = None) -> HubConfig:
    """Store the enrollment and prove it works before claiming success."""
    cfg = HubConfig(url.rstrip("/"), key, consented=int(time.time()))
    HubClient(cfg).manifest()        # raises HubError on a bad key or URL
    cfg.save(path)
    return cfg
