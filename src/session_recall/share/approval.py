"""Approval and the send process — the only door out, default deny.

The worker fills the outbox; nothing leaves it without a human decision that
quotes the candidate's exact version hash. approve() refuses a mismatched
version, so any edit or regeneration kills previously seen /ok text (the
TOCTOU rule: an approval blesses a blob, not an intention). dispatch_approved()
is the single place in the codebase that turns candidates into outgoing
envelopes; the worker module does not import it and cannot reach it.
"""

import time
from pathlib import Path

from .envelope import make_response
from .identity import Identity
from .trust import TrustStore
from .worker import Candidate, list_pending, load_candidate, set_status

PENDING_TTL_S = 24 * 3600


def preview(cand: Candidate, redact: bool | None = None) -> str:
    """The message the owner sees. `redact` hides the answer body — forced on
    when the scanner flagged anything, because flagged text must not travel
    through a third-party notification channel (gate §6)."""
    redact = bool(cand.findings) if redact is None else redact
    lines = [f"[{cand.id} v{cand.version}] request from {cand.peer_name}",
             f"question: {cand.question}"]
    if cand.task:
        lines.append(f'stated task (sender text, unverified): "{cand.task}"')
    if cand.findings:
        kinds = ", ".join(sorted({f["kind"] for f in cand.findings}))
        lines.append(f"⚠ SECRET FLAGS: {kinds}")
    if redact:
        lines.append(f"[answer withheld from this channel — review locally: "
                     f"session-recall share show {cand.id}]")
    else:
        lines.append(f"--- answer v{cand.version} ---\n{cand.text}")
    lines.append(f"approve: /ok {cand.version}   decline: /no <reason>")
    return "\n".join(lines)


def approve(share_dir: Path, cand_id: str, version: str) -> Candidate | None:
    """None unless `version` matches the stored candidate exactly."""
    cand = load_candidate(share_dir, cand_id)
    if cand is None or cand.status != "pending" or version != cand.version:
        return None
    return set_status(share_dir, cand_id, "approved")


def reject(share_dir: Path, cand_id: str, reason: str = "") -> Candidate | None:
    cand = load_candidate(share_dir, cand_id)
    if cand is None or cand.status != "pending":
        return None
    cand.status = "rejected"
    cand.text = f"(declined) {reason}".strip()  # what, if anything, goes back
    from .worker import _write_candidate
    _write_candidate(share_dir, cand)
    return cand


def expire_stale(share_dir: Path, now: float | None = None) -> list[Candidate]:
    """A three-day-old /ok must not fire a forgotten answer."""
    now = time.time() if now is None else now
    out = []
    for cand in list_pending(share_dir):
        if now - cand.created_at > PENDING_TTL_S:
            out.append(set_status(share_dir, cand.id, "expired"))
    return out


def dispatch(identity: Identity, trust: TrustStore, share_dir: Path,
             transport, statuses: tuple[str, ...] = ("approved", "rejected")) -> list[Candidate]:
    """Send everything the human has decided on. Approved candidates carry the
    answer; rejected ones carry the decline note so the peer is not left
    hanging. Peers revoked since the decision drop silently — fail closed."""
    from .worker import OUTBOX_DIR
    import json
    sent = []
    d = share_dir / OUTBOX_DIR
    if not d.is_dir():
        return sent
    for p in sorted(d.glob("*.json")):
        cand = Candidate(**json.loads(p.read_text()))
        if cand.status not in statuses:
            continue
        peer = trust.get_by_address(cand.peer_address)
        if peer is None:
            set_status(share_dir, cand.id, "dropped-revoked")
            continue
        transport.post_mail(peer.address,
                            make_response(identity, peer, cand.text,
                                          in_reply_to=cand.reply_nonce))
        # keep the audit trail honest: a delivered decline is not an answer
        set_status(share_dir, cand.id,
                   "sent" if cand.status == "approved" else "declined-sent")
        sent.append(cand)
    return sent
