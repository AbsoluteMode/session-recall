"""Telegram Bot API over stdlib urllib — sendMessage and getUpdates are two
endpoints and do not justify a dependency.

Privacy posture (gate §6): everything sent through here transits Telegram's
servers, so flagged answers are redacted upstream (approval.preview). The bot
config binds to exactly one owner chat; updates from any other chat are
discarded before parsing.

Formatting posture: our own chrome (labels, the /ok line) is the ONLY thing
allowed to carry markup. Every untrusted string — the peer's name, their
question, their stated task, and snippets pulled from the index — goes inside
a fenced code block via `fence()`. That does three jobs at once: markup in
untrusted text renders literally instead of forging structure, Telegram does
not auto-linkify inside code blocks (so a URL sitting in someone's transcript
cannot become a tappable link in the approval channel), and the owner can tell
our words from quoted material at a glance.
"""

import json
import os
import stat
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TG_FILE = "tg.json"
_TIMEOUT_S = 35   # long-poll friendly
MARKDOWN = "MarkdownV2"

_ESCAPE = r"_*[]()~`>#+-=|{}.!\\"


def escape_md(text: str) -> str:
    """MarkdownV2 escaping for short strings we place outside code blocks."""
    return "".join("\\" + c if c in _ESCAPE else c for c in text)


def inline_code(text: str) -> str:
    """Same guarantees as fence() for short strings that belong on one line —
    a code span is also markup-inert and never auto-linkified."""
    return "`" + text.replace("\\", "\\\\").replace("`", "\\`") + "`"


def fence(text: str, lang: str = "") -> str:
    """Wrap untrusted text in a code block it cannot escape.

    Only backslash and backtick can break out of a fence, so those two are the
    entire escape set here — everything else (asterisks, brackets, URLs) is
    inert inside the block.
    """
    safe = text.replace("\\", "\\\\").replace("`", "\\`")
    return f"```{lang}\n{safe}\n```"


@dataclass
class TgConfig:
    token: str
    chat_id: int | None
    offset: int = 0


def load_config(share_dir: Path) -> TgConfig | None:
    p = share_dir / TG_FILE
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    return TgConfig(token=raw["token"], chat_id=raw.get("chat_id"),
                    offset=raw.get("offset", 0))


def save_config(share_dir: Path, cfg: TgConfig) -> None:
    share_dir.mkdir(parents=True, exist_ok=True)
    p = share_dir / TG_FILE
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"token": cfg.token, "chat_id": cfg.chat_id,
                   "offset": cfg.offset}, f)


class TgApi:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def _call(self, method: str, **params) -> dict:
        data = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}).encode()
        with urllib.request.urlopen(f"{self.base}/{method}", data=data,
                                    timeout=_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
        return payload.get("result", {})

    def send_message(self, chat_id: int, text: str, reply_to: int | None = None,
                     parse_mode: str | None = None) -> int | None:
        # 4096 is Telegram's hard cap; truncate rather than fail the preview
        result = self._call("sendMessage", chat_id=chat_id, text=text[:4096],
                            reply_to_message_id=reply_to, parse_mode=parse_mode)
        return result.get("message_id") if isinstance(result, dict) else None

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        result = self._call("getUpdates", offset=offset, timeout=timeout)
        return result if isinstance(result, list) else []
