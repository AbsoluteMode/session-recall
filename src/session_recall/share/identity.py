"""Local share identity: one signing keypair, one box keypair, one address.

Lives OUTSIDE the index DB on purpose: rebuilding the index must never rotate
keys, and the MCP server exposes no tool that touches this file — identity and
trust management is human-only CLI (gate §2).
"""

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from nacl.public import PrivateKey
from nacl.signing import SigningKey

from .crypto import b64, unb64, new_address

IDENTITY_FILE = "identity.json"


def _write_private(path: Path, payload: dict) -> None:
    """0600 from the first byte — never a window where the file is world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)


@dataclass
class Identity:
    name: str
    address: str
    signing_key: SigningKey
    box_key: PrivateKey
    created_at: float

    @property
    def sign_pk_b64(self) -> str:
        return b64(bytes(self.signing_key.verify_key))

    @property
    def box_pk_b64(self) -> str:
        return b64(bytes(self.box_key.public_key))

    def public_bundle(self) -> dict:
        """What the other side learns about us during pairing. Public keys and a
        self-chosen name only — nothing here is sensitive."""
        return {"name": self.name, "address": self.address,
                "sign_pk": self.sign_pk_b64, "box_pk": self.box_pk_b64}


def create(share_dir: Path, name: str) -> Identity:
    path = share_dir / IDENTITY_FILE
    if path.exists():
        raise FileExistsError(
            f"share identity already exists at {path}; delete it only if you "
            "mean to abandon every pairing made with it")
    ident = Identity(name=name, address=new_address(),
                     signing_key=SigningKey.generate(),
                     box_key=PrivateKey.generate(), created_at=time.time())
    _write_private(path, {
        "v": 1, "name": ident.name, "address": ident.address,
        "sign_sk": b64(bytes(ident.signing_key)),
        "box_sk": b64(bytes(ident.box_key)),
        "created_at": ident.created_at,
    })
    return ident


def load(share_dir: Path) -> Identity | None:
    path = share_dir / IDENTITY_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return Identity(name=data["name"], address=data["address"],
                    signing_key=SigningKey(unb64(data["sign_sk"])),
                    box_key=PrivateKey(unb64(data["box_sk"])),
                    created_at=data.get("created_at", 0.0))
