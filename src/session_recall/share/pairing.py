"""Pairing ceremony: invite code out-of-band, key bundles through the relay,
SAS verified by voice, then an explicit human `trust` step.

Why this shape (gate §1): the invite code is a one-time 256-bit secretbox key
that never touches the relay, so the relay cannot read or substitute bundles —
a MITM without the code fails MAC verification. The SAS read-out on top makes
a stolen/guessed slot visible to the humans. TOFU ends there; afterwards every
request is signed by the exchanged keys.

Flow:  A: invite  →  code to B out-of-band  →  B: join <code>  →  A: complete
       both read the same SAS aloud          →  both: trust
"""

import json
import time
from dataclasses import dataclass
from pathlib import Path

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.utils import random as nacl_random

from .crypto import b32, unb32, b64, unb64, canonical, new_address, sas_code, group
from .identity import Identity

INVITE_TTL_S = 600
_ID_LEN, _KEY_LEN = 5, SecretBox.KEY_SIZE

PENDING_INVITE_FILE = "pending_invite.json"
PENDING_PEER_FILE = "pending_peer.json"


class PairingError(Exception):
    pass


@dataclass
class PairingResult:
    bundle: dict   # the other side's public bundle
    sas: str


def _seal(key: bytes, bundle: dict) -> bytes:
    return SecretBox(key).encrypt(canonical({"bundle": bundle, "ts": time.time()}),
                                  nacl_random(SecretBox.NONCE_SIZE))


def _open(key: bytes, blob: bytes, ttl_s: float = INVITE_TTL_S) -> dict:
    try:
        payload = json.loads(SecretBox(key).decrypt(blob))
    except CryptoError as exc:
        raise PairingError("invite code does not match this exchange "
                           "(wrong code, or someone tampered with the slot)") from exc
    if time.time() - payload["ts"] > ttl_s:
        raise PairingError("invite expired — start a fresh one")
    return payload["bundle"]


def start_invite(identity: Identity, transport, share_dir: Path) -> str:
    """Returns the code to hand over out-of-band (chat, voice, paper).

    The invite mints a fresh inbox address for whoever answers it, so this
    ten-minute window is also where the pair's private channel is born: the code
    is what the first joiner spends, and the address they get back is known to
    them alone."""
    invite_id = nacl_random(_ID_LEN)
    key = nacl_random(_KEY_LEN)
    local = new_address()
    transport.put_slot(f"pair-a-{b32(invite_id)}",
                       _seal(key, identity.public_bundle(local)))
    (share_dir / PENDING_INVITE_FILE).write_text(json.dumps(
        {"invite_id": b32(invite_id), "key": b64(key), "local_address": local,
         "created_at": time.time()}))
    return group(b32(invite_id + key))


def join(identity: Identity, transport, share_dir: Path, code: str) -> PairingResult:
    raw = unb32(code)
    if len(raw) != _ID_LEN + _KEY_LEN:
        raise PairingError("that does not look like an invite code")
    invite_id, key = b32(raw[:_ID_LEN]), raw[_ID_LEN:]
    blob = transport.get_slot(f"pair-a-{invite_id}")
    if blob is None:
        raise PairingError("invite not found — expired, already used, or wrong code")
    their = _open(key, blob)
    local = new_address()
    transport.put_slot(f"pair-b-{invite_id}",
                       _seal(key, identity.public_bundle(local)))
    return _finish(identity, share_dir, their, local)


def complete_invite(identity: Identity, transport, share_dir: Path) -> PairingResult:
    pending_path = share_dir / PENDING_INVITE_FILE
    if not pending_path.exists():
        raise PairingError("no pending invite — run `share invite` first")
    pending = json.loads(pending_path.read_text())
    if time.time() - pending["created_at"] > INVITE_TTL_S:
        pending_path.unlink()
        raise PairingError("invite expired — start a fresh one")
    blob = transport.get_slot(f"pair-b-{pending['invite_id']}")
    if blob is None:
        raise PairingError("the other side has not joined yet")
    their = _open(unb64(pending["key"]), blob)
    pending_path.unlink()
    return _finish(identity, share_dir, their, pending.get("local_address", ""))


def _finish(identity: Identity, share_dir: Path, their: dict,
            local_address: str = "") -> PairingResult:
    sas = sas_code(unb64(identity.sign_pk_b64), unb64(their["sign_pk"]))
    # Parked until the human confirms the SAS and runs `share trust` — pairing
    # never writes the trust store itself.
    (share_dir / PENDING_PEER_FILE).write_text(json.dumps(
        {"bundle": their, "sas": sas, "local_address": local_address,
         "ts": time.time()}))
    return PairingResult(bundle=their, sas=sas)


def pending_peer(share_dir: Path) -> dict | None:
    path = share_dir / PENDING_PEER_FILE
    return json.loads(path.read_text()) if path.exists() else None


def clear_pending_peer(share_dir: Path) -> None:
    path = share_dir / PENDING_PEER_FILE
    if path.exists():
        path.unlink()
