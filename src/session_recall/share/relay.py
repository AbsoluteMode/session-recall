"""Minimal relay: dumb, blind, disposable.

It stores two kinds of opaque blobs — pairing slots and mail envelopes — and
can read neither (secretbox/box seal them client-side). Losing the relay loses
nothing but undelivered mail; trust never depends on it (gate: transports are
untrusted).

What it still must defend, because crypto upstream cannot:
- **mail theft-by-consume**: fetching an inbox destroys it, so fetch requires
  an Ed25519 signature over a fresh timestamp. The first authenticated fetch
  binds address→key (first-write-wins; addresses are 128 random bits, so a
  squatter would have to guess the address before its owner's first fetch).
- **garbage floods**: size/count caps and TTLs. POST stays anonymous on
  purpose — authenticating senders would hand the relay the social graph,
  and the receiver's own gate drops junk anyway.
- **metadata hygiene**: requests are logged as method+status only, never the
  path — the path is the address.

stdlib http.server on purpose: four routes do not justify a framework.
"""

import json
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from nacl.exceptions import CryptoError
from nacl.signing import VerifyKey

from .crypto import b64, unb64, canonical

MAX_BLOB = 64 * 1024
MAX_MAILBOX = 100
SLOT_TTL_S = 3600
MAIL_TTL_S = 7 * 86400
FETCH_SIG_WINDOW_S = 60

_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")


class RelayStore:
    """Filesystem-backed so a restart drops nothing. Layout mirrors
    FileTransport (slots/, mail/<addr>/) plus addrs.json for fetch auth."""

    def __init__(self, root: Path, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        self._addrs_path = self.root / "addrs.json"
        self.addrs: dict[str, str] = (
            json.loads(self._addrs_path.read_text(encoding="utf-8")) if self._addrs_path.exists() else {})

    def _prune(self, directory: Path, ttl: float) -> None:
        if not directory.is_dir():
            return
        now = self.clock()
        for p in directory.iterdir():
            if now - p.stat().st_mtime > ttl:
                p.unlink(missing_ok=True)

    # -- slots ----------------------------------------------------------------
    def put_slot(self, slot: str, blob: bytes) -> None:
        d = self.root / "slots"
        d.mkdir(parents=True, exist_ok=True)
        self._prune(d, SLOT_TTL_S)
        (d / slot).write_bytes(blob)

    def get_slot(self, slot: str) -> bytes | None:
        p = self.root / "slots" / slot
        self._prune(self.root / "slots", SLOT_TTL_S)
        return p.read_bytes() if p.exists() else None

    # -- mail -----------------------------------------------------------------
    def post_mail(self, address: str, blob: bytes) -> None:
        d = self.root / "mail" / address
        d.mkdir(parents=True, exist_ok=True)
        self._prune(d, MAIL_TTL_S)
        if len(list(d.iterdir())) >= MAX_MAILBOX:
            return  # full mailbox drops silently; the cap is anti-flood
        (d / f"{int(self.clock() * 1000):015d}-{secrets.token_hex(4)}").write_bytes(blob)

    def consume_mail(self, address: str) -> list[bytes]:
        d = self.root / "mail" / address
        if not d.is_dir():
            return []
        self._prune(d, MAIL_TTL_S)
        out = []
        for p in sorted(d.iterdir()):
            out.append(p.read_bytes())
            p.unlink()
        return out

    # -- fetch auth -----------------------------------------------------------
    def check_fetch(self, address: str, pk_b64: str, ts: float, sig_b64: str) -> bool:
        if abs(self.clock() - ts) > FETCH_SIG_WINDOW_S:
            return False
        known = self.addrs.get(address)
        pk = known or pk_b64          # first fetch enrolls, later ones must match
        try:
            VerifyKey(unb64(pk)).verify(
                canonical({"action": "fetch", "address": address, "ts": ts}),
                unb64(sig_b64))
        except (CryptoError, ValueError, TypeError):
            return False
        if known is None:
            self.addrs[address] = pk_b64
            self.root.mkdir(parents=True, exist_ok=True)
            self._addrs_path.write_text(json.dumps(self.addrs), encoding="utf-8")
        return True


def fetch_auth_headers(identity, address: str) -> dict[str, str]:
    """Client side of check_fetch — kept next to it so they cannot drift."""
    ts = time.time()
    sig = identity.signing_key.sign(
        canonical({"action": "fetch", "address": address, "ts": ts})).signature
    return {"X-Share-Pk": identity.sign_pk_b64, "X-Share-Ts": str(ts),
            "X-Share-Sig": b64(sig)}


def make_handler(store: RelayStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "session-recall-relay"

        def log_message(self, fmt, *args):  # method+status only; paths are addresses
            print(f"{self.command} -> {getattr(self, '_status', '?')}", flush=True)

        def _reply(self, code: int, body: bytes = b"") -> None:
            self._status = code
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> bytes | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BLOB:
                return None
            return self.rfile.read(length)

        def _name(self, prefix: str) -> str | None:
            path = urlparse(self.path).path
            if not path.startswith(prefix):
                return None
            name = path[len(prefix):]
            return name if _NAME_RE.match(name) else None

        def do_PUT(self):
            slot = self._name("/slot/")
            body = self._read_body()
            if slot is None or body is None:
                return self._reply(413 if body is None else 404)
            store.put_slot(slot, body)
            self._reply(200)

        def do_POST(self):
            address = self._name("/mail/")
            body = self._read_body()
            if address is None or body is None:
                return self._reply(413 if body is None else 404)
            store.post_mail(address, body)
            self._reply(200)  # always 200: existence of inboxes is not disclosed

        def do_GET(self):
            slot = self._name("/slot/")
            if slot is not None:
                blob = store.get_slot(slot)
                return self._reply(404) if blob is None else self._reply(200, blob)
            address = self._name("/mail/")
            if address is not None:
                try:
                    ok = store.check_fetch(
                        address, self.headers.get("X-Share-Pk", ""),
                        float(self.headers.get("X-Share-Ts", "0")),
                        self.headers.get("X-Share-Sig", ""))
                except ValueError:
                    ok = False
                # bad auth and empty inbox are indistinguishable on purpose
                items = store.consume_mail(address) if ok else []
                return self._reply(200, json.dumps([b64(i) for i in items]).encode())
            self._reply(404)

    return Handler


def serve(port: int, data_dir: Path, host: str = "127.0.0.1") -> None:
    """Localhost by default: the relay speaks plain HTTP, so a public bind
    would put addresses and blobs on the wire unencrypted. Production runs it
    behind a TLS terminator; pass host explicitly to override."""
    server = ThreadingHTTPServer((host, port), make_handler(RelayStore(data_dir)))
    print(f"relay listening on {host}:{port}, data in {data_dir}", flush=True)
    server.serve_forever()
