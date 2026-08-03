"""Transports carry opaque encrypted blobs; they are 100% untrusted.

Everything that crosses a transport is sealed and signed by the layers above
(pairing: secretbox under the invite key; mail: box + Ed25519). A malicious
relay can drop or replay bytes — both are handled above — but can neither read
nor forge. That is why three interchangeable implementations are fine:
in-memory for tests, a shared directory for two accounts on one machine or a
synced folder, and the HTTP relay (server lands in the next PR).
"""

import json
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

_HTTP_TIMEOUT_S = 10


class Transport(Protocol):
    def put_slot(self, slot: str, blob: bytes) -> None: ...
    def get_slot(self, slot: str) -> bytes | None: ...
    def post_mail(self, address: str, blob: bytes) -> None: ...
    def fetch_mail(self, address: str) -> list[bytes]: ...


class InMemoryTransport:
    def __init__(self):
        self.slots: dict[str, bytes] = {}
        self.mail: dict[str, list[bytes]] = {}

    def put_slot(self, slot: str, blob: bytes) -> None:
        self.slots[slot] = blob

    def get_slot(self, slot: str) -> bytes | None:
        return self.slots.get(slot)

    def post_mail(self, address: str, blob: bytes) -> None:
        self.mail.setdefault(address, []).append(blob)

    def fetch_mail(self, address: str) -> list[bytes]:
        return self.mail.pop(address, [])


class FileTransport:
    """A directory both parties can reach (same machine, synced folder)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put_slot(self, slot: str, blob: bytes) -> None:
        d = self.root / "slots"
        d.mkdir(parents=True, exist_ok=True)
        (d / slot).write_bytes(blob)

    def get_slot(self, slot: str) -> bytes | None:
        p = self.root / "slots" / slot
        return p.read_bytes() if p.exists() else None

    def post_mail(self, address: str, blob: bytes) -> None:
        d = self.root / "mail" / address
        d.mkdir(parents=True, exist_ok=True)
        (d / secrets.token_hex(8)).write_bytes(blob)

    def fetch_mail(self, address: str) -> list[bytes]:
        d = self.root / "mail" / address
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir()):
            out.append(p.read_bytes())
            p.unlink()
        return out


class HttpRelayTransport:
    """Client for the minimal relay service. stdlib urllib on purpose — the
    relay API is four endpoints and not worth a dependency.

    `identity` is needed for fetch_mail only: consuming an inbox is
    destructive, so the relay demands a signature (see relay.check_fetch);
    everything else stays anonymous."""

    def __init__(self, base_url: str, identity=None):
        self.base = base_url.rstrip("/")
        self.identity = identity

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None) -> bytes | None:
        all_headers = {"Content-Type": "application/octet-stream", **(headers or {})}
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=all_headers)
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def put_slot(self, slot: str, blob: bytes) -> None:
        self._request("PUT", f"/slot/{slot}", blob)

    def get_slot(self, slot: str) -> bytes | None:
        return self._request("GET", f"/slot/{slot}")

    def post_mail(self, address: str, blob: bytes) -> None:
        self._request("POST", f"/mail/{address}", blob)

    def fetch_mail(self, address: str) -> list[bytes]:
        from .relay import fetch_auth_headers
        headers = fetch_auth_headers(self.identity, address) if self.identity else {}
        raw = self._request("GET", f"/mail/{address}", headers=headers)
        if not raw:
            return []
        items = json.loads(raw)
        import base64
        return [base64.b64decode(i) for i in items]


# No relay is baked in: an open-source install must never talk to a server
# its user did not choose — not even a blind one. WHICH transport two peers
# use is coordination they settle when they pair (the relay only ever carries
# blobs sealed and signed client-side, so the choice is about metadata and
# availability, not message trust).
#
# Deliberately NOT taken from the pairing bundle either: a bundle is
# peer-supplied data, and a relay URL inside it would let a malicious bundle
# point this client at an attacker's server. Configuring the transport is
# manual by design.


def from_env(env: dict, identity=None) -> Transport | None:
    """Explicit relay URL wins; a shared directory is the zero-infra
    alternative; nothing configured means NO transport at all — the CLI
    surfaces what to set. `SESSION_RECALL_RELAY_URL=none` (or `off`)
    disables the relay while leaving a configured directory usable."""
    url = (env.get("SESSION_RECALL_RELAY_URL") or "").strip()
    if url and url.lower() not in ("none", "off"):
        return HttpRelayTransport(url, identity=identity)
    root = env.get("SESSION_RECALL_SHARE_TRANSPORT_DIR")
    if root:
        return FileTransport(Path(root))
    return None
