"""Signed, end-to-end encrypted request/response envelopes + the fail-closed
inbound gate: signature, freshness, replay, rate.

open_request/open_response return None for anything unacceptable — unknown or
revoked sender, bad signature, stale timestamp, replayed nonce, rate excess.
Silent drop is deliberate (gate §3): an error reply would confirm the address
exists and leak which check failed.
"""

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import VerifyKey

from .crypto import b64, unb64, canonical
from .identity import Identity
from .trust import Peer, TrustStore

REQ_TTL_S = 600            # clock skew between two laptops + relay latency
RATE_PER_MIN = 3           # gate §4
RATE_PER_DAY = 30          # 3/min alone would still allow 4320/day — the pump
STATE_FILE = "state.json"


class ShareState:
    """Replay nonces and per-peer rate counters. JSON, not the index DB: this
    must survive index rebuilds and stay out of any LLM-reachable tool."""

    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            raw = json.loads(path.read_text())
            self.seen: dict[str, float] = raw.get("seen", {})
            self.rate: dict[str, list[float]] = raw.get("rate", {})
        else:
            self.seen, self.rate = {}, {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            json.dump({"seen": self.seen, "rate": self.rate}, f)
        os.replace(tmp, self.path)

    def replay(self, nonce: str, now: float) -> bool:
        self.seen = {n: t for n, t in self.seen.items() if now - t < 2 * REQ_TTL_S}
        if nonce in self.seen:
            return True
        self.seen[nonce] = now
        self._save()
        return False

    def rate_exceeded(self, address: str, now: float) -> bool:
        window = [t for t in self.rate.get(address, []) if now - t < 86400]
        minute = [t for t in window if now - t < 60]
        if len(minute) >= RATE_PER_MIN or len(window) >= RATE_PER_DAY:
            self.rate[address] = window
            self._save()
            return True
        window.append(now)
        self.rate[address] = window
        self._save()
        return False


@dataclass
class Incoming:
    peer: Peer
    kind: str          # "req" | "resp"
    body: dict         # req: {question, task}; resp: {text}
    nonce: str
    in_reply_to: str | None
    ts: float


def _sealed(identity: Identity, peer_box_pk: str, kind: str, body: dict,
            to_address: str, in_reply_to: str | None) -> bytes:
    box = Box(identity.box_key, PublicKey(unb64(peer_box_pk)))
    envelope = {
        "v": 1, "kind": kind,
        "from": identity.address, "to": to_address,
        "ts": time.time(), "nonce": b64(os.urandom(16)),
        "ct": b64(box.encrypt(canonical(body))),
    }
    if in_reply_to:
        envelope["in_reply_to"] = in_reply_to
    sig = identity.signing_key.sign(canonical(envelope)).signature
    envelope["sig"] = b64(sig)
    return json.dumps(envelope).encode()


def make_request(identity: Identity, peer: Peer | dict, question: str,
                 task: str = "") -> bytes:
    box_pk = peer.box_pk if isinstance(peer, Peer) else peer["box_pk"]
    address = peer.address if isinstance(peer, Peer) else peer["address"]
    return _sealed(identity, box_pk, "req",
                   {"question": question, "task": task}, address, None)


def make_response(identity: Identity, peer: Peer, text: str,
                  in_reply_to: str) -> bytes:
    return _sealed(identity, peer.box_pk, "resp", {"text": text},
                   peer.address, in_reply_to)


def open_incoming(identity: Identity, trust: TrustStore, state: ShareState,
                  raw: bytes, now: float | None = None) -> Incoming | None:
    """The single inbound gate. Order matters: cheap and unauthenticated checks
    first, the rate counter only after the signature proves the sender —
    otherwise a stranger could spend a peer's quota by spoofing `from`."""
    now = time.time() if now is None else now
    try:
        env = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(env, dict) or env.get("v") != 1:
        return None
    if env.get("kind") not in ("req", "resp"):
        return None
    if env.get("to") != identity.address:
        return None

    peer = trust.get_by_address(env.get("from", ""))
    if peer is None:                      # unknown or revoked → same silence
        return None
    sig = env.pop("sig", None)
    if not sig:
        return None
    try:
        VerifyKey(unb64(peer.sign_pk)).verify(canonical(env), unb64(sig))
    except (CryptoError, ValueError):
        return None

    ts = env.get("ts", 0)
    if abs(now - ts) > REQ_TTL_S:
        return None
    if state.replay(env["nonce"], now):
        return None
    if env["kind"] == "req" and state.rate_exceeded(peer.address, now):
        return None

    try:
        box = Box(identity.box_key, PublicKey(unb64(peer.box_pk)))
        body = json.loads(box.decrypt(unb64(env["ct"])))
    except (CryptoError, ValueError):
        return None
    return Incoming(peer=peer, kind=env["kind"], body=body, nonce=env["nonce"],
                    in_reply_to=env.get("in_reply_to"), ts=ts)
