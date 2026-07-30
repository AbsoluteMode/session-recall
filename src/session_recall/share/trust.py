"""Trust store: who may ask, and which projects are shareable at all.

Written only by the CLI in a human's hands. The MCP server and the (future)
answer worker get read access at most — an LLM must be physically unable to
enroll a peer or widen scope (gate §2: tokens are made by people).

mode is stored per peer but v1 enforces `accept` everywhere: the bypass ramp
(scanner hard-block, per-peer scope, volume caps) is deliberately not built yet
— see the decision doc's "Отвергли".
"""

import json
import os
import stat
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

TRUST_FILE = "trust.json"


@dataclass
class Peer:
    name: str
    address: str
    sign_pk: str
    box_pk: str
    mode: str = "accept"
    added_at: float = 0.0
    revoked: bool = False


@dataclass
class _State:
    peers: list = field(default_factory=list)
    allowed_projects: list = field(default_factory=list)


class TrustStore:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            raw = json.loads(path.read_text())
            self._state = _State(
                peers=[Peer(**p) for p in raw.get("peers", [])],
                allowed_projects=list(raw.get("allowed_projects", [])))
        else:
            self._state = _State()

    def _save(self) -> None:
        """Atomic + 0600: a half-written trust store must never exist, and the
        peer list is nobody's business but the owner's."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w") as f:
            json.dump({"peers": [asdict(p) for p in self._state.peers],
                       "allowed_projects": self._state.allowed_projects}, f, indent=2)
        os.replace(tmp, self.path)

    # -- peers ---------------------------------------------------------------
    def add(self, peer: Peer) -> None:
        if self.get_by_address(peer.address, include_revoked=True):
            raise ValueError(f"peer with address {peer.address} already present")
        peer.added_at = peer.added_at or time.time()
        self._state.peers.append(peer)
        self._save()

    def peers(self, include_revoked: bool = False) -> list[Peer]:
        return [p for p in self._state.peers if include_revoked or not p.revoked]

    def get_by_address(self, address: str, include_revoked: bool = False) -> Peer | None:
        for p in self._state.peers:
            if p.address == address and (include_revoked or not p.revoked):
                return p
        return None

    def revoke(self, name_or_address: str) -> Peer | None:
        """Flag, don't delete: a revoked key must stay known so its envelopes
        keep failing closed instead of looking like a stranger's."""
        for p in self._state.peers:
            if not p.revoked and name_or_address in (p.name, p.address):
                p.revoked = True
                self._save()
                return p
        return None

    # -- shareable scope -----------------------------------------------------
    def allowed_projects(self) -> list[str]:
        return list(self._state.allowed_projects)

    def allow_project(self, project: str) -> None:
        if project not in self._state.allowed_projects:
            self._state.allowed_projects.append(project)
            self._save()

    def disallow_project(self, project: str) -> None:
        if project in self._state.allowed_projects:
            self._state.allowed_projects.remove(project)
            self._save()
