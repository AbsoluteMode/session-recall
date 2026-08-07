"""Conversations: a question and everything that followed it.

One-shot Q&A forces the asker to state the perfect question first time and gives
the owner no way to say "which OS?" or to answer from memory when the index has
nothing. A thread fixes both — and the owner writing a reply themselves is the
most valuable path of all, because often the real answer was never in the index.

The thread id lives inside the *encrypted* body, not the signed envelope, so the
relay learns nothing about conversation structure — only that some address got
mail. Each side keeps its own log; there is no shared server-side state.

Approval rule, and the reason the log records who authored each turn:
a machine-drafted answer needs /ok, while a message the owner typed is already
authorized by the act of typing it. Authorship is the gate.
"""

import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

THREADS_DIR = "threads"
MAX_TURNS = 40          # a thread this long has become a channel, not a question
IDLE_CLOSE_S = 14 * 86400


@dataclass
class Turn:
    author: str        # "peer" | "owner" | "worker"
    text: str
    ts: float
    kind: str = "message"   # message | question | answer | decline


@dataclass
class Thread:
    id: str
    peer_address: str
    peer_name: str
    created_at: float
    turns: list = field(default_factory=list)
    closed: bool = False

    def append(self, author: str, text: str, kind: str = "message") -> None:
        self.turns.append(asdict(Turn(author=author, text=text, ts=time.time(),
                                      kind=kind)))

    def context(self, limit: int = 6) -> list:
        """Recent turns for the composer. Untrusted every round: an injection
        planted in turn 1 is still sitting here on turn 9, so callers must fence
        this the same way they fence a fresh request."""
        return self.turns[-limit:]

    def should_close(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if len(self.turns) >= MAX_TURNS:
            return True
        last = self.turns[-1]["ts"] if self.turns else self.created_at
        return now - last > IDLE_CLOSE_S


def new_id() -> str:
    return secrets.token_hex(6)


def _path(share_dir: Path, thread_id: str) -> Path:
    return share_dir / THREADS_DIR / f"{thread_id}.json"


def save(share_dir: Path, thread: Thread) -> None:
    d = share_dir / THREADS_DIR
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    path = _path(share_dir, thread.id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(thread), f, indent=2, ensure_ascii=False)


def load(share_dir: Path, thread_id: str) -> Thread | None:
    path = _path(share_dir, thread_id)
    if not path.exists():
        return None
    return Thread(**json.loads(path.read_text(encoding="utf-8")))


def open_or_create(share_dir: Path, thread_id: str, peer_address: str,
                   peer_name: str) -> Thread:
    """Threads are created by whichever side speaks first; the other side
    materialises the same id on receipt."""
    existing = load(share_dir, thread_id)
    if existing is not None:
        return existing
    return Thread(id=thread_id, peer_address=peer_address, peer_name=peer_name,
                  created_at=time.time())


def listing(share_dir: Path) -> list[Thread]:
    d = share_dir / THREADS_DIR
    if not d.is_dir():
        return []
    threads = [Thread(**json.loads(p.read_text(encoding="utf-8"))) for p in d.glob("*.json")]
    return sorted(threads, key=lambda t: t.turns[-1]["ts"] if t.turns
                  else t.created_at, reverse=True)
