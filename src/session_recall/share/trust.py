"""Trust store: who may ask, what each of them may see, and where they reach us.

Written only by the CLI in a human's hands. The MCP server and the answer
worker get read access at most — an LLM must be physically unable to enroll a
peer or widen scope (gate §2: tokens are made by people).

Three things live per peer, and each answers a different question:

- **petname** — what *we* call them. The name a peer picks for itself is their
  text and can say anything, including "egor"; the petname is ours, unique, and
  it is what every prompt and preview refers to. Impersonation by naming stops
  being possible.
- **local_address** — the inbox *we* minted for this one peer during pairing.
  Nobody else knows it, so the relay cannot tell that two senders are talking to
  the same person, and revoking a peer stops us listening on that address at all
  rather than merely flagging their envelopes.
- **projects** — what this peer may see. Scope is per contact because what one
  colleague should know is not what another should: `share allow X --to egor`.
  The `--to all` bucket exists for projects that are genuinely open to every
  contact, and it is spelled out loud rather than being the silent default.

Nothing here defends against the peer's own machine being stolen: their signing
key goes with it and the thief simply is them. What per-peer addressing buys is
that the blast radius stops at that one channel, and `share revoke` closes it.

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
    name: str                 # the peer's own claim — untrusted text, never a key
    address: str              # where we send to them (they minted it for us)
    sign_pk: str
    box_pk: str
    mode: str = "accept"
    added_at: float = 0.0
    revoked: bool = False
    petname: str = ""         # our name for them; unique, used everywhere
    local_address: str = ""   # the inbox we minted for this peer alone
    projects: list = field(default_factory=list)   # what they specifically may see

    @property
    def label(self) -> str:
        return self.petname or self.name


@dataclass
class _State:
    peers: list = field(default_factory=list)
    allowed_projects: list = field(default_factory=list)
    paused: bool = False


class TrustStore:
    def __init__(self, path: Path):
        self.path = path
        if path.exists():
            raw = json.loads(path.read_text())
            self._state = _State(
                peers=[Peer(**p) for p in raw.get("peers", [])],
                allowed_projects=list(raw.get("allowed_projects", [])),
                paused=bool(raw.get("paused", False)))
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
                       "allowed_projects": self._state.allowed_projects,
                       "paused": self._state.paused}, f, indent=2)
        os.replace(tmp, self.path)

    # -- peers ---------------------------------------------------------------
    def add(self, peer: Peer) -> None:
        if self.get_by_address(peer.address, include_revoked=True):
            raise ValueError(f"peer with address {peer.address} already present")
        if peer.petname and self.get(peer.petname, include_revoked=True):
            raise ValueError(
                f"{peer.petname!r} is already taken — petnames are how you tell "
                "contacts apart, so each one must be unique")
        peer.added_at = peer.added_at or time.time()
        self._state.peers.append(peer)
        self._save()

    def peers(self, include_revoked: bool = False) -> list[Peer]:
        return [p for p in self._state.peers if include_revoked or not p.revoked]

    def get_by_address(self, address: str, include_revoked: bool = False) -> Peer | None:
        if not address:
            return None
        for p in self._state.peers:
            if p.address == address and (include_revoked or not p.revoked):
                return p
        return None

    def get(self, ref: str, include_revoked: bool = False) -> Peer | None:
        """By petname first — it is the name the owner chose and the only one
        that cannot collide. The peer's self-chosen name is a last resort so
        pre-petname stores keep working."""
        for p in self.peers(include_revoked):
            if p.petname and p.petname == ref:
                return p
        for p in self.peers(include_revoked):
            if ref in (p.address, p.name):
                return p
        return None

    def revoke(self, ref: str) -> Peer | None:
        """Flag, don't delete: a revoked key must stay known so its envelopes
        keep failing closed instead of looking like a stranger's. The inbox we
        minted for them drops out of `inbox_addresses` in the same move, so we
        stop even collecting their mail."""
        peer = self.get(ref)
        if peer is None:
            return None
        peer.revoked = True
        self._save()
        return peer

    def inbox_addresses(self, identity) -> list[str]:
        """Every address we listen on: the per-peer inboxes plus the identity's
        own, which is what pre-pairing installs and legacy peers still use."""
        seen, out = set(), []
        for address in [identity.address] + [p.local_address for p in self.peers()]:
            if address and address not in seen:
                seen.add(address)
                out.append(address)
        return out

    # -- shareable scope -----------------------------------------------------
    def allowed_projects(self) -> list[str]:
        """The `--to all` bucket: open to every contact, present and future."""
        return list(self._state.allowed_projects)

    def allow_project(self, project: str, peer: Peer | None = None) -> None:
        if peer is None:
            if project not in self._state.allowed_projects:
                self._state.allowed_projects.append(project)
                self._save()
            return
        if project not in peer.projects:
            peer.projects.append(project)
            self._save()

    def disallow_project(self, project: str, peer: Peer | None = None) -> None:
        if peer is None:
            if project in self._state.allowed_projects:
                self._state.allowed_projects.remove(project)
                self._save()
            return
        if project in peer.projects:
            peer.projects.remove(project)
            self._save()

    def projects_for(self, peer: Peer | None) -> list[str]:
        """What this peer may actually see: their own grants plus the everyone
        bucket. Default deny — a freshly trusted peer with no grants and an
        empty bucket sees nothing at all."""
        if peer is None:
            return []
        out = list(self._state.allowed_projects)
        for project in peer.projects:
            if project not in out:
                out.append(project)
        return out

    # -- kill switch ---------------------------------------------------------
    @property
    def paused(self) -> bool:
        return self._state.paused

    def set_paused(self, paused: bool) -> None:
        """Stop answering without unpairing anyone. Paused means we do not even
        collect mail, so questions wait in the relay's mailbox (7 days) instead
        of being consumed and dropped."""
        self._state.paused = bool(paused)
        self._save()
