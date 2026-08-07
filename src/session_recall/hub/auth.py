"""Who is talking to the hub.

A key looks like `sr_<owner>_<32 hex>`. The owner travels in the clear on
purpose — an operator reading a config file, a log line or a `key list` should
know whose key it is without a lookup — and the random half is the secret.

Only a SHA-256 of the whole key is stored. A hub database that leaks therefore
hands out no access, which matters more here than in the solo product: this
file sits next to a pooled index of the whole team's history.

The owner in the key is a claim, not the trust: it counts only because the
stored hash proves the bearer holds a key the operator issued for that name.
Ingest writes into the tree of the owner resolved this way, never a name the
client sends in the request body — otherwise anyone could write into anyone's
history.

Deliberately absent in v1: expiry and per-key rate limits. The hub is reachable
only over TLS by people the operator issued keys to, and revocation is
immediate; adding clocks and counters before there is a second failure mode to
defend against would be ceremony.
"""

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

from .. import perms

_KEY_RE = re.compile(r"^sr_([a-z0-9][a-z0-9-]{0,31})_([0-9a-f]{32})$")
_BEARER_RE = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def parse_owner(key: str) -> str | None:
    """The owner a key claims. Says nothing about whether the key is valid."""
    found = _KEY_RE.match(key or "")
    return found.group(1) if found else None


def bearer(header: str | None) -> str | None:
    """Pull the credential out of an Authorization header."""
    found = _BEARER_RE.match((header or "").strip())
    return found.group(1) if found else None


class KeyStore:
    """Issued keys, on disk as JSON keyed by hash.

    Re-read on every verify: the file is tiny, the OS caches it, and an
    operator who revokes a key expects the next request to fail — not the next
    restart.
    """

    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        perms.protect(tmp)
        tmp.replace(self.path)   # atomic: a crash mid-write never truncates the store

    def issue(self, owner: str, note: str = "") -> str:
        """Mint a key for `owner` and return it — the only time it exists in
        readable form. Nothing recoverable is kept, so a lost key is reissued,
        never recovered."""
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,31}$", owner or ""):
            raise ValueError(
                f"bad owner name {owner!r}: lowercase letters, digits and dashes")
        key = f"sr_{owner}_{secrets.token_hex(16)}"
        data = self._load()
        data[key_hash(key)] = {
            "owner": owner, "note": note,
            "issued": int(self.clock()), "revoked": None,
        }
        self._save(data)
        return key

    def verify(self, header: str | None) -> str | None:
        """Owner for a valid Authorization header, else None.

        Lookup is by hash of the presented key, so no comparison walks the
        secret byte by byte and a wrong key cannot be distinguished from an
        unknown one by timing.
        """
        key = bearer(header)
        if not key or not parse_owner(key):
            return None
        record = self._load().get(key_hash(key))
        if not record or record.get("revoked"):
            return None
        return record["owner"]

    def revoke(self, selector: str) -> int:
        """Revoke by key id (the short hash shown in `list`) or by owner name.

        Owner-wide revocation is the common case and the reason it is one call:
        somebody leaves, and every device they ever enrolled must stop working
        in one command, not one command per laptop.
        """
        data = self._load()
        hit = 0
        for digest, record in data.items():
            if record.get("revoked"):
                continue
            if digest.startswith(selector) or record.get("owner") == selector:
                record["revoked"] = int(self.clock())
                hit += 1
        if hit:
            self._save(data)
        return hit

    def listing(self) -> list[dict]:
        """Issued keys, newest first. Never contains anything usable as a key."""
        rows = [{"id": digest[:8], **record}
                for digest, record in self._load().items()]
        return sorted(rows, key=lambda r: r.get("issued", 0), reverse=True)
