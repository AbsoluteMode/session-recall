"""The owner's approval loop: previews out, /ok — /no back, then dispatch.

Channel-agnostic: `api` is anything with send_message/get_updates (TgApi in
production, a fake in tests). Binding rules:

- only messages from the single bound owner chat are read at all;
- /ok must be a REPLY to a preview and must quote the version hash from it —
  the reply names the candidate, the quoted hash names the exact blob, so an
  ambiguous or automatic approval has nothing to grab;
- the candidate reference is parsed from the quoted preview text, not from
  memory, so a daemon restart loses nothing;
- every decision path funnels into approval.py — this module owns no crypto
  and no envelope construction.
"""

import re
import time
from pathlib import Path

from . import approval
from .envelope import ShareState
from .identity import Identity
from .telegram import TgConfig, save_config
from .trust import TrustStore
from .worker import Searcher, poll_once

_REF_RE = re.compile(r"^\[([0-9a-f]{8}) v([0-9a-f]{8})\]")
_USAGE = ("usage: reply to a preview with `/ok <version>`, `/no <reason>`, "
          "or just write your own answer and it goes as-is")


def _cand_ref(message: dict) -> tuple[str, str] | None:
    quoted = (message.get("reply_to_message") or {}).get("text", "")
    m = _REF_RE.match(quoted)
    return (m.group(1), m.group(2)) if m else None


class NotifyLoop:
    def __init__(self, api, identity: Identity, trust: TrustStore,
                 state: ShareState, transport, searcher: Searcher,
                 share_dir: Path, cfg: TgConfig, composer=None):
        self.api, self.identity, self.trust = api, identity, trust
        self.state, self.transport, self.searcher = state, transport, searcher
        self.share_dir, self.cfg, self.composer = share_dir, cfg, composer

    def _say(self, text: str, reply_to: int | None = None) -> None:
        self.api.send_message(self.cfg.chat_id, text, reply_to=reply_to)

    def _say_preview(self, cand) -> None:
        """Markdown first, plain text if Telegram rejects it. A preview that
        fails to render must still arrive — silence would look exactly like
        'no one asked anything', and nothing can be approved unseen."""
        from .telegram import MARKDOWN
        try:
            self.api.send_message(self.cfg.chat_id,
                                  approval.preview(cand, markdown=True),
                                  parse_mode=MARKDOWN)
        except Exception:
            self._say(approval.preview(cand))

    def _handle(self, message: dict) -> None:
        text = (message.get("text") or "").strip()
        mid = message.get("message_id")
        if not text:
            return
        ref = _cand_ref(message)
        if ref is None:
            # a command needs a preview to point at; anything else is noise
            return self._say(_USAGE, reply_to=mid) if text.startswith("/") else None
        cand_id, _preview_version = ref  # the reply names the candidate…

        if not text.startswith("/"):
            # plain text replied into a thread is the owner answering in their
            # own words — authorship is the authorization, so it just goes
            from .worker import load_candidate
            cand = load_candidate(self.share_dir, cand_id)
            if cand is None or not cand.thread:
                return self._say(f"nothing to reply to under {cand_id}", reply_to=mid)
            ok = approval.own_message(self.identity, self.trust, self.share_dir,
                                      self.transport, cand.thread, text)
            return self._say(f"sent to {cand.peer_name}" if ok else
                             "not sent: thread closed or peer revoked", reply_to=mid)

        if text.startswith("/ok"):
            parts = text.split()
            if len(parts) != 2:
                return self._say(_USAGE, reply_to=mid)
            # …while the TYPED hash is what gets compared to the stored one:
            # retyping it from the preview is the deliberate human step, and a
            # stale preview after an edit simply cannot supply the right hash.
            cand = approval.approve(self.share_dir, cand_id, parts[1])
            if cand is None:
                return self._say(
                    f"not approved: version mismatch or not pending "
                    f"(review: session-recall share show {cand_id})", reply_to=mid)
            self._say(f"approved {cand_id} v{cand.version} — sending", reply_to=mid)
        else:
            reason = text[3:].strip()
            cand = approval.reject(self.share_dir, cand_id, reason)
            if cand is None:
                return self._say(f"nothing pending under {cand_id}", reply_to=mid)
            self._say(f"declined {cand_id}", reply_to=mid)

    def tick(self, now: float | None = None) -> dict:
        stats = {"previews": 0, "handled": 0, "sent": 0, "expired": 0}

        for cand in poll_once(self.identity, self.trust, self.state,
                              self.transport, self.searcher, self.share_dir,
                              composer=self.composer):
            self._say_preview(cand)
            stats["previews"] += 1

        stats["expired"] = len(approval.expire_stale(self.share_dir, now))

        for update in self.api.get_updates(self.cfg.offset):
            self.cfg.offset = max(self.cfg.offset, update.get("update_id", 0) + 1)
            message = update.get("message") or {}
            if (message.get("chat") or {}).get("id") != self.cfg.chat_id:
                continue          # anyone else talking to the bot is noise
            self._handle(message)
            stats["handled"] += 1
        save_config(self.share_dir, self.cfg)

        for cand in approval.dispatch(self.identity, self.trust,
                                      self.share_dir, self.transport):
            if cand.status != "rejected":
                self._say(f"sent to {cand.peer_name}: {cand.id} v{cand.version}")
            stats["sent"] += 1
        return stats

    def run_forever(self, interval_s: float = 5.0) -> None:
        while True:
            self.tick()
            time.sleep(interval_s)
