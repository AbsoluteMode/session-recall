"""The hub's HTTP surface.

stdlib http.server, like the share relay: the route table is small and a
framework would buy nothing here. TLS, timeouts and the public name are
nginx's job — this listens on localhost.

Routes:
    GET  /healthz              liveness, unauthenticated, says nothing about content
    POST /v1/ingest/manifest   what we already hold for the caller
    POST /v1/ingest            append (or replace) one transcript's bytes
    POST /v1/search            semantic recall over the pooled index

Everything except /healthz requires `Authorization: Bearer <key>`, and the
owner comes from the key — never from the request body. A client cannot write
into, or claim to be, another member's history.

Concurrency shapes two decisions here. Requests are threaded, so each thread
gets its OWN sqlite connection (a connection may not cross threads), and the
indexer writes to the same database while searches read it, so the hub opens
in WAL mode with a busy timeout instead of serializing everything behind one
writer.
"""

import base64
import gzip
import json
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from ..store import Store
from ..embed import make_embedder
from ..rerank import make_reranker
from ..retrieve import Recall
from ..timefmt import date_range_to_epoch, humanize_ts
from . import ask as hub_ask
from . import storage
from .auth import KeyStore
from .masking import SecretMap

MAX_BODY = 16 * 1024 * 1024      # one ingest chunk plus base64 overhead
_BUSY_TIMEOUT_MS = 10_000
_UNSET = object()                # "not built yet", distinct from "no composer"


class Hub:
    """Server-side state: where the data lives and how to search it."""

    def __init__(self, root: Path, keys: KeyStore | None = None,
                 recall_factory=None, composer=_UNSET):
        self._composer = composer
        self.root = Path(root)
        self.transcripts = self.root / "transcripts"
        self.db_path = self.root / "index.db"
        self.keys = keys or KeyStore(self.root / "keys.json")
        self.ledger = storage.Ledger(self.root / "state")
        self.secrets_path = self.root / "secrets.json"
        self._recall_factory = recall_factory or self._build_recall
        self._local = threading.local()
        self._secret_map: SecretMap | None = None
        self._secret_mtime = -1.0
        self._secret_lock = threading.Lock()

    @property
    def composer(self):
        """The model that writes `ask` answers, or None for the digest path.

        Built once and cached: `make_composer` only reads configuration, but
        the hub's privacy rule has to be attached at construction, and doing
        that per request would be a silent invitation to forget it.
        """
        if self._composer is _UNSET:
            from ..share.compose import make_composer
            self._composer = make_composer(system_extra=hub_ask.PRIVACY_RULE)
        return self._composer

    @property
    def secret_map(self) -> SecretMap:
        """The masking map, reloaded whenever the file changes.

        `hub secrets refresh` runs on a timer in a separate process, and a map
        that only loaded at boot would quietly stop covering credentials added
        since — the failure mode being "the new API key is in the index".
        """
        try:
            mtime = self.secrets_path.stat().st_mtime
        except OSError:
            mtime = 0.0
        with self._secret_lock:
            if self._secret_map is None or mtime != self._secret_mtime:
                self._secret_map = SecretMap.load(self.secrets_path)
                self._secret_mtime = mtime
            return self._secret_map

    def _build_recall(self) -> Recall:
        # The service is a READER, never a writer: the indexer owns writes.
        # Anything else and sqlite-vec's shadow tables lose the indexer's
        # writes — measured, not theoretical (see Store.__init__).
        if not self.db_path.exists():
            # A hub with nothing indexed yet has no file to open read-only.
            # Create the schema once, then drop the writable handle.
            Store(self.db_path).close()
        store = Store(self.db_path, read_only=True)
        store.db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        return Recall(store, make_embedder(), make_reranker())

    @property
    def recall(self) -> Recall:
        """One Recall per thread — sqlite connections are not shareable."""
        found = getattr(self._local, "recall", None)
        if found is None:
            found = self._local.recall = self._recall_factory()
        return found


def _anchor(a) -> dict:
    d = asdict(a)
    d["when_human"] = humanize_ts(a.when, int(time.time()))
    return d


def _range(body: dict) -> tuple[int | None, int | None]:
    """Time bounds from either form.

    A human caller sends calendar dates and a timezone; the MCP proxy has
    already resolved those on the client (where the user's timezone actually
    is) and sends epochs. Accepting both keeps the resolution in one place per
    caller instead of guessing a server-side timezone.
    """
    if body.get("start_ts") is not None or body.get("end_ts") is not None:
        return body.get("start_ts"), body.get("end_ts")
    return date_range_to_epoch(
        body.get("start_date"), body.get("end_date"),
        body.get("timezone"), on_date=body.get("on_date"))


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "claude-recall"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # --- plumbing -------------------------------------------------

        def log_message(self, fmt, *args):
            # Method, status and owner only. Request paths carry project and
            # session names — team history metadata that does not belong in a
            # log that outlives the request.
            owner = getattr(self, "_owner", "-")
            print(f"{self.command} {self.path.split('?')[0][:40]} "
                  f"owner={owner} {args[1] if len(args) > 1 else ''}".strip(),
                  flush=True)

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY:
                return None
            raw = self.rfile.read(length)
            if (self.headers.get("Content-Encoding") or "").lower() == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except (OSError, EOFError, gzip.BadGzipFile):
                    return None
            try:
                parsed = json.loads(raw)
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None

        def _auth(self) -> str | None:
            owner = hub.keys.verify(self.headers.get("Authorization"))
            self._owner = owner or "-"
            return owner

        # --- routes ---------------------------------------------------

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            route = {
                "/v1/ingest/manifest": self._manifest,
                "/v1/ingest": self._ingest,
                "/v1/search": self._search,
                "/v1/expand": self._expand,
                "/v1/step": self._step,
                "/v1/grep": self._grep,
                "/v1/recent": self._recent,
                "/v1/ask": self._ask,
            }.get(self.path)
            if route is None:
                return self._send(404, {"error": "not found"})
            owner = self._auth()
            if owner is None:
                # No hint about which half was wrong: unknown, malformed and
                # revoked keys are one answer.
                return self._send(401, {"error": "unauthorized"})
            body = self._body()
            if body is None:
                return self._send(400, {"error": "bad request body"})
            try:
                route(owner, body)
            except storage.OffsetMismatch as mismatch:
                # 409, with our size, so the client can resume without a full
                # re-upload of a months-long transcript.
                self._send(409, {"error": "offset mismatch", "size": mismatch.actual})
            except storage.UnsafePath as bad:
                self._send(400, {"error": str(bad)})
            except Exception as unexpected:                # noqa: BLE001
                self._send(500, {"error": type(unexpected).__name__})

        def _manifest(self, owner: str, body: dict) -> None:
            self._send(200, {"files": storage.manifest(
                hub.transcripts, owner, hub.ledger)})

        def _ingest(self, owner: str, body: dict) -> None:
            path = body.get("path")
            offset = body.get("offset", 0)
            if not isinstance(path, str) or not isinstance(offset, int):
                return self._send(400, {"error": "path and offset required"})
            try:
                data = base64.b64decode(body.get("data") or "", validate=True)
            except (ValueError, TypeError):
                return self._send(400, {"error": "data must be base64"})
            # How much of the CLIENT's file this covers. It differs from
            # len(data) whenever the client redacted before sending, and the
            # client's own count is the only correct one — the hub never sees
            # the original bytes. Trusting it is safe: the only thing a wrong
            # number corrupts is that member's own resume point.
            raw_len = body.get("raw_len")
            if raw_len is not None and (not isinstance(raw_len, int) or raw_len < 0):
                return self._send(400, {"error": "raw_len must be a non-negative int"})
            # Mask BEFORE the bytes touch the disk: a credential that lands in
            # the transcript tree is already in the backup and already indexed
            # by the time anyone notices. surrogateescape keeps a transcript
            # with odd bytes byte-exact instead of failing the upload.
            text = data.decode("utf-8", errors="surrogateescape")
            masked, hits = hub.secret_map.mask(text)
            stored = masked.encode("utf-8", errors="surrogateescape")
            size = storage.append(hub.transcripts, owner, path, offset, stored,
                                  hub.ledger,
                                  received_len=raw_len if raw_len is not None
                                  else len(data))
            self._send(200, {"size": size, "masked": hits})

        def _search(self, owner: str, body: dict) -> None:
            query = body.get("query")
            if not isinstance(query, str) or not query.strip():
                return self._send(400, {"error": "query required"})
            start_ts, end_ts = _range(body)
            hits = hub.recall.recall_search(
                query, k=int(body.get("k") or 10),
                scope_cwd=body.get("scope_cwd"), source=body.get("source"),
                start_ts=start_ts, end_ts=end_ts)
            self._send(200, {"anchors": [_anchor(a) for a in hits],
                             "degraded": getattr(hits, "degraded", None)})

        def _expand(self, owner: str, body: dict) -> None:
            turns = hub.recall.expand_around(
                body.get("session_id", ""), body.get("uuid", ""),
                int(body.get("before", 2)), int(body.get("after", 2)),
                source=body.get("source"))
            self._send(200, {"turns": [asdict(t) for t in turns]})

        def _step(self, owner: str, body: dict) -> None:
            try:
                turns = hub.recall.step(
                    body.get("session_id", ""), body.get("uuid", ""),
                    body.get("direction", "next"), int(body.get("count", 1)),
                    source=body.get("source"))
            except ValueError as bad:
                return self._send(400, {"error": str(bad)})
            self._send(200, {"turns": [asdict(t) for t in turns]})

        def _grep(self, owner: str, body: dict) -> None:
            pattern = body.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                return self._send(400, {"error": "pattern required"})
            start_ts, end_ts = _range(body)
            hits = hub.recall.grep(
                pattern, body.get("session_id"), scope_cwd=body.get("scope_cwd"),
                source=body.get("source"), limit=int(body.get("limit") or 100),
                start_ts=start_ts, end_ts=end_ts)
            self._send(200, {"anchors": [_anchor(a) for a in hits]})

        def _recent(self, owner: str, body: dict) -> None:
            start_ts, end_ts = _range(body)
            self._send(200, {"sessions": hub.recall.recent_sessions(
                scope_cwd=body.get("scope_cwd"), limit=int(body.get("limit") or 10),
                source=body.get("source"), start_ts=start_ts, end_ts=end_ts)})

        def _ask(self, owner: str, body: dict) -> None:
            question = body.get("question")
            if not isinstance(question, str) or not question.strip():
                return self._send(400, {"error": "question required"})
            self._send(200, hub_ask.answer(
                hub, question, k=int(body.get("k") or hub_ask.MAX_FRAGMENTS),
                scope_cwd=body.get("scope_cwd"), source=body.get("source"),
                composer=hub.composer))

    return Handler
