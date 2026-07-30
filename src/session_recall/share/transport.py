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
    relay API is four endpoints and not worth a dependency."""

    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes | None:
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers={"Content-Type": "application/octet-stream"})
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
        raw = self._request("GET", f"/mail/{address}?consume=1")
        if not raw:
            return []
        items = json.loads(raw)
        import base64
        return [base64.b64decode(i) for i in items]


def from_env(env: dict) -> Transport | None:
    """Relay URL wins; a shared directory is the zero-infra fallback."""
    url = env.get("SESSION_RECALL_RELAY_URL")
    if url:
        return HttpRelayTransport(url)
    root = env.get("SESSION_RECALL_SHARE_TRANSPORT_DIR")
    if root:
        return FileTransport(Path(root))
    return None
