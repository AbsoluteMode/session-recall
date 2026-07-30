"""Thin helpers over libsodium (PyNaCl) for the share protocol.

Gate rule (docs/decisions/2026-07-30-p2p-sharing-v1-security-gate.md): no
hand-rolled primitives. Everything here is Ed25519 / X25519 / XSalsa20-Poly1305
/ BLAKE2b straight from libsodium; this module only fixes encodings and the SAS
derivation so both sides compute them identically.
"""

import base64
import json
import secrets

from nacl import hash as nacl_hash
from nacl.encoding import RawEncoder


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def b32(data: bytes) -> str:
    """Lowercase base32 without padding — addresses and invite codes. Survives
    voice, chat clients and URL paths, unlike base64's mixed case and '/'. """
    return base64.b32encode(data).decode("ascii").rstrip("=").lower()


def unb32(text: str) -> bytes:
    clean = text.replace("-", "").replace(" ", "").upper()
    return base64.b32decode(clean + "=" * (-len(clean) % 8))


def new_address() -> str:
    """128 random bits. Defeats enumeration/bruteforce of inboxes; it is NOT a
    secret and NOT authentication — signatures are (gate §3)."""
    return b32(secrets.token_bytes(16))


def canonical(obj: dict) -> bytes:
    """Stable bytes for signing: sorted keys, no whitespace. Both ends must
    produce the identical byte string or signatures cannot verify."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sas_code(*parts: bytes) -> str:
    """Short authentication string both humans read aloud after pairing.

    Order-independent (parts are sorted) so inviter and joiner compute the same
    code without agreeing who is 'first'. 8 digits in two groups — enough to
    make a relay-substitution attack visible, short enough to actually be read.
    """
    digest = nacl_hash.blake2b(b"\x00".join(sorted(parts)),
                               digest_size=8, encoder=RawEncoder)
    num = int.from_bytes(digest, "big") % 10**8
    return f"{num:08d}"[:4] + " " + f"{num:08d}"[4:]


def group(text: str, size: int = 4) -> str:
    """xxxx-xxxx-… presentation for codes the user copies around."""
    return "-".join(text[i:i + size] for i in range(0, len(text), size))
