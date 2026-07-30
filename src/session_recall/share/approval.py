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


ANSWER_BUDGET = 1400   # chars of answer shown in the channel, before truncation
SNIPPET_BUDGET = 320   # per fragment, so five fragments cannot bury the verdict


def _answer_digest(cand: Candidate) -> tuple[str, int]:
    """Answer trimmed for a phone screen. Per-fragment caps first (so one long
    chunk cannot crowd out the rest), then a whole-message cap. Returns the
    text and how many characters were withheld — the owner must know the
    channel is showing less than what would actually be sent."""
    if getattr(cand, "composed", False) or not cand.chunks:
        # a composed answer IS the deliverable — show it, don't rebuild it from
        # the fragments it was written from
        if len(cand.text) <= ANSWER_BUDGET:
            return cand.text, 0
        return cand.text[:ANSWER_BUDGET].rstrip() + " …", len(cand.text) - ANSWER_BUDGET
    parts = []
    for c in cand.chunks:
        snippet = c["snippet"]
        if len(snippet) > SNIPPET_BUDGET:
            snippet = snippet[:SNIPPET_BUDGET].rstrip() + " …"
        parts.append(f"{c['project']} · {c['session_id'][:8]} · {c['role']}\n{snippet}")
    body = "\n\n".join(parts)
    if len(body) <= ANSWER_BUDGET:
        return body, max(0, len(cand.text) - len(body))
    cut = body[:ANSWER_BUDGET].rstrip() + " …"
    return cut, len(cand.text) - len(cut)


def preview(cand: Candidate, redact: bool | None = None,
            markdown: bool = False) -> str:
    """The message the owner sees. `redact` hides the answer body — forced on
    when the scanner flagged anything, because flagged text must not travel
    through a third-party notification channel (gate §6).

    With markdown=True the layout uses MarkdownV2: our labels carry the markup,
    every untrusted string sits in a code fence (see telegram.fence), and the
    /ok line is a code span so Telegram makes it tap-to-copy.
    """
    from .telegram import escape_md, fence, inline_code

    redact = bool(cand.findings) if redact is None else redact
    plain = not markdown
    esc = (lambda s: s) if plain else escape_md
    block = (lambda s: s) if plain else fence
    span = (lambda s: s) if plain else inline_code

    out = []
    if plain:
        out.append(f"[{cand.id} v{cand.version}] request from {cand.peer_name}")
        out.append(f"question: {cand.question}")
    else:
        out.append(f"📥 *request from* {span(cand.peer_name)}")
        out.append(f"*question*\n{block(cand.question)}")

    # Both are the sender's own words — shown so the owner can judge the ask,
    # fenced so they cannot pose as ours.
    if cand.task:
        label = "what they are doing (sender text, unverified)"
        out.append(f'{label}: "{cand.task}"' if plain
                   else f"*{esc(label)}*\n{block(cand.task)}")
    if getattr(cand, "problem", ""):
        label = "problem they hit (sender text, unverified)"
        out.append(f'{label}: "{cand.problem}"' if plain
                   else f"*{esc(label)}*\n{block(cand.problem)}")

    if cand.findings:
        kinds = ", ".join(sorted({f["kind"] for f in cand.findings}))
        out.append(f"⚠ SECRET FLAGS: {kinds}" if plain
                   else f"⚠️ *secret flags*: {esc(kinds)}")

    if redact:
        hint = f"session-recall share show {cand.id}"
        out.append(f"[answer withheld from this channel — review locally: {hint}]"
                   if plain else
                   f"_answer withheld from this channel_ — review locally:\n"
                   f"{span(hint)}")
    elif plain:
        out.append(f"--- answer v{cand.version} ---\n{cand.text}")
    else:
        body, withheld = _answer_digest(cand)
        # our own chrome needs escaping too: parentheses and dots are reserved
        # in MarkdownV2, and an unescaped one makes Telegram reject the whole
        # message rather than render it oddly
        # who wrote it matters to the reader: a composed answer is prose the
        # model derived from the fragments, a digest is the fragments verbatim
        counts = "written from " if getattr(cand, "composed", False) else "raw "
        counts += f"{len(cand.chunks)} fragment(s)"
        if withheld > 0:
            counts += f" · {withheld} more chars on send"
        out.append(f"*answer* · {esc(counts)} · `v{esc(cand.version)}`\n{block(body)}")

    if plain:
        out.append(f"approve: /ok {cand.version}   decline: /no <reason>")
    else:
        out.append(f"*approve* — reply to this message with:\n"
                   f"`/ok {esc(cand.version)}`\n"
                   f"*decline*: `/no <reason>`")
    return "\n".join(out) if plain else "\n\n".join(out)


def own_message(identity: Identity, trust: TrustStore, share_dir: Path,
                transport, thread_id: str, text: str) -> bool:
    """The owner answering in their own words. No approval step: they authored
    it, which IS the authorization — a second confirmation on your own typing
    is ritual, not a gate. Everything else (revoked peer, closed thread) still
    fails closed."""
    from . import thread as thread_mod
    convo = thread_mod.load(share_dir, thread_id)
    if convo is None or convo.closed or not text.strip():
        return False
    peer = trust.get_by_address(convo.peer_address)
    if peer is None:
        return False
    transport.post_mail(peer.address,
                        make_response(identity, peer, text.strip(),
                                      in_reply_to="", thread=thread_id))
    convo.append("owner", text.strip())
    thread_mod.save(share_dir, convo)
    return True


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
                                          in_reply_to=cand.reply_nonce,
                                          thread=cand.thread,
                                          sources=len(cand.chunks)))
        # keep the audit trail honest: a delivered decline is not an answer
        set_status(share_dir, cand.id,
                   "sent" if cand.status == "approved" else "declined-sent")
        sent.append(cand)
    return sent
