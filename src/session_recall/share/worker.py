"""The caged answerer: inbound envelopes → candidate answers, nothing else.

Capability cage (gate §5): this module can (a) query the local index read-only
through an injected `searcher` and (b) write candidate files into the outbox.
It deliberately imports no send path, no approval channel, no shell, and no
generic filesystem access — a prompt injection that fully controlled this code
path could still only shape TEXT that lands in front of the owner. Sending is
the send process's job after `/ok` (next PR); default-deny is structural.

Two more gate invariants live here:
- scope: only anchors from projects THIS peer may see enter a candidate
  (their own grants plus the `--to all` bucket; default deny — no grants
  means every answer comes back empty);
- the worker leaves no transcripts, so nothing it processes can re-enter the
  index and become a stored injection later.

The candidate is assembled deterministically from recall anchors (snippets +
provenance). An LLM summariser can later rewrite `text` — the envelope of the
candidate (chunks, scanner flags, version hash, approval binding) stays as is.
"""

import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable

from nacl.encoding import RawEncoder
from nacl import hash as nacl_hash

from . import thread as thread_mod
from .ask import retrieval_query
from .crypto import canonical
from .envelope import Incoming, ShareState, open_incoming
from .identity import Identity
from .scanner import scan
from .trust import TrustStore

OUTBOX_DIR = "outbox"
MAX_CHUNKS = 5
MAX_SNIPPET = 700

# searcher(query, k) -> anchors; injected so the worker can be driven by the
# real Recall or by a stub in tests, and so this module never owns an embedder.
Searcher = Callable[[str, int], Iterable]


@dataclass
class Candidate:
    id: str
    peer_name: str
    peer_address: str
    question: str
    task: str
    reply_nonce: str          # binds the eventual response to the exact request
    created_at: float
    text: str
    chunks: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    status: str = "pending"   # pending | approved | rejected | expired
    version: str = ""         # /ok must quote this — approval of an exact blob
    problem: str = ""         # symptoms the asker reported; last for compatibility
    composed: bool = False    # True when an LLM wrote `text` from the fragments
    thread: str = ""          # conversation this answer belongs to

    def compute_version(self) -> str:
        digest = nacl_hash.blake2b(
            canonical({"text": self.text, "chunks": self.chunks}),
            digest_size=4, encoder=RawEncoder)
        return digest.hex()


def _outbox(share_dir: Path) -> Path:
    d = share_dir / OUTBOX_DIR
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def _write_candidate(share_dir: Path, cand: Candidate) -> Path:
    path = _outbox(share_dir) / f"{cand.id}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(cand), f, indent=2, ensure_ascii=False)
    return path


def _digest(chunks: list) -> str:
    """Deterministic fallback answer: the fragments themselves, with provenance.
    Never leaves the machine to be produced, so it is what the owner gets when
    no composer is configured or the provider is unreachable."""
    return "\n\n".join(f"[{c['project']} · {c['session_id'][:8]} · {c['role']}]\n"
                       f"{c['snippet']}" for c in chunks)


def build_candidate(incoming: Incoming, searcher: Searcher,
                    allowed_projects: list[str], composer=None,
                    turns: list | None = None) -> Candidate:
    body = {"question": str(incoming.body.get("question", ""))[:2000],
            "task": str(incoming.body.get("task", ""))[:500],
            "problem": str(incoming.body.get("problem", ""))[:1000]}
    anchors = list(searcher(retrieval_query(body), 20)) if allowed_projects else []
    picked = [a for a in anchors if a.project in allowed_projects][:MAX_CHUNKS]

    chunks = [{"project": a.project, "session_id": a.session_id, "uuid": a.uuid,
               "role": a.role, "snippet": a.snippet[:MAX_SNIPPET],
               "score": a.score, "source": a.source} for a in picked]

    composed = False
    if not chunks:
        text = ("(nothing found within what this contact may see — "
                "see `session-recall share allow`)")
    else:
        written = composer(body, chunks, turns) if composer else None
        text, composed = (written, True) if written else (_digest(chunks), False)

    # the scanner runs on the sources too: a composed answer can look clean
    # while the model was reading secret-adjacent material, and the owner
    # should know to open that one locally
    findings = [asdict(f) for f in scan(text)]
    seen = {(f["kind"], f["excerpt"]) for f in findings}
    for c in chunks:
        for f in scan(c["snippet"]):
            if (f.kind, f.excerpt) not in seen:
                seen.add((f.kind, f.excerpt))
                findings.append({**asdict(f), "in": "source"})

    cand = Candidate(
        id=secrets.token_hex(4), peer_name=incoming.peer.label,
        peer_address=incoming.peer.address, question=body["question"],
        task=body["task"], problem=body["problem"],
        reply_nonce=incoming.nonce, created_at=time.time(), text=text,
        chunks=chunks, findings=findings, composed=composed,
        thread=str(incoming.body.get("thread", ""))[:32])
    cand.version = cand.compute_version()
    return cand


def poll_once(identity: Identity, trust: TrustStore, state: ShareState,
              transport, searcher: Searcher, share_dir: Path,
              composer=None) -> list[Candidate]:
    """One sweep over every inbox we listen on (one per pairing plus the
    legacy identity address). Invalid envelopes vanish inside open_incoming
    (silent drop); valid requests become pending candidates on disk.

    Paused means paused *before* the fetch: consuming mail is destructive, so a
    paused owner leaves questions parked in the relay (its TTL applies) instead
    of eating and dropping them."""
    if trust.paused:
        return []
    out = []
    for inbox in trust.inbox_addresses(identity):
        for raw in transport.fetch_mail(inbox):
            incoming = open_incoming(identity, trust, state, raw)
            if incoming is None or incoming.kind != "req":
                continue
            thread_id = str(incoming.body.get("thread", ""))[:32] or thread_mod.new_id()
            convo = thread_mod.open_or_create(share_dir, thread_id,
                                              incoming.peer.address,
                                              incoming.peer.label)
            if convo.closed or convo.should_close():
                convo.closed = True      # a thread this old is a standing channel
                thread_mod.save(share_dir, convo)
                continue
            cand = build_candidate(incoming, searcher,
                                   trust.projects_for(incoming.peer),
                                   composer=composer, turns=convo.context())
            cand.thread = thread_id
            convo.append("peer", cand.question, kind="question")
            thread_mod.save(share_dir, convo)
            _write_candidate(share_dir, cand)
            out.append(cand)
    return out


# -- outbox reading, shared with the approval flow (next PR) -----------------
def list_pending(share_dir: Path) -> list[Candidate]:
    d = share_dir / OUTBOX_DIR
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        cand = Candidate(**json.loads(p.read_text()))
        if cand.status == "pending":
            out.append(cand)
    return out


def load_candidate(share_dir: Path, cand_id: str) -> Candidate | None:
    p = share_dir / OUTBOX_DIR / f"{cand_id}.json"
    return Candidate(**json.loads(p.read_text())) if p.exists() else None


def set_status(share_dir: Path, cand_id: str, status: str) -> Candidate | None:
    cand = load_candidate(share_dir, cand_id)
    if cand is None:
        return None
    cand.status = status
    _write_candidate(share_dir, cand)
    return cand
