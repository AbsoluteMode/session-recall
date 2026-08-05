"""Secret scanner for outgoing candidates — dumb regexes on purpose.

The gate's reasoning (§ превью/сканер): an LLM classifier would be one more
prompt-injectable surface, while key formats are rigid strings. This is a
tripwire, not a guarantee — its job is to catch the retrieval accidents
(an env dump sitting next to the deploy discussion) and to survive any text
an injection can compose. Findings never block by themselves in v1; they are
flags the owner sees before approving.
"""

import re
from dataclasses import dataclass

# ORDER MATTERS for `redact`, which replaces on first match: a specific
# pattern must precede any broader one that also matches it, or the
# replacement carries the wrong label. `sk-ant-…` is also a valid `sk-…`, so
# anthropic-key comes first. (`scan` reports every match and is order-blind.)
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("private-key-pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("telegram-bot-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("password-assignment", re.compile(   # [\w-]* prefix: POSTGRES_PASSWORD=…
        r"(?i)\b[\w-]*(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*\S{8,}")),
    ("connection-string", re.compile(r"\b\w+://[^/\s:]+:[^@\s]+@[^\s]+")),
]


@dataclass
class Finding:
    kind: str
    excerpt: str   # masked: enough to recognise, not enough to leak via the flag


def _mask(match: str) -> str:
    return match[:8] + "…" if len(match) > 8 else match


def scan(text: str) -> list[Finding]:
    findings = []
    for kind, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            findings.append(Finding(kind=kind, excerpt=_mask(m.group(0))))
    return findings


# Patterns precise enough to cut with nobody looking. `password-assignment` is
# deliberately NOT here: `\w*(token|secret|key)\s*[=:]\s*\S{8,}` matches any
# JSON field, config line or sentence shaped like "token: …", and on the first
# real corpus it fired 27324 times against ~1900 for every real key format
# combined — it deletes the surrounding work, not the secrets. It stays in
# `scan`, where a human reads the finding and decides.
_AUTO_REDACT = {
    "aws-access-key", "anthropic-key", "openai-key", "github-token",
    "gitlab-token", "slack-token", "jwt", "private-key-pem",
    "telegram-bot-token", "connection-string",
}


def redact(text: str) -> tuple[str, int]:
    """Replace confidently-identified secrets with `[REDACTED:<kind>]`.

    `scan` flags for a human who then decides; this one decides for them, and
    exists for the path where no human is in the loop: a hub client uploading
    a member's transcripts. There the same finding cannot become a preview to
    approve — the alternative to cutting it is shipping it — so the tripwire
    is wired to act, and therefore has to be far more conservative about what
    it fires on than the flagging path.

    Still a tripwire, not a guarantee: it knows FORMATS, so it catches an
    `sk-ant-…` and cannot catch a password. Values the team actually keeps in
    Doppler are handled by exact match on the hub (`hub/masking.py`); the two
    layers miss different things on purpose.
    """
    count = 0
    for kind, pattern in _PATTERNS:
        if kind not in _AUTO_REDACT:
            continue
        text, hits = pattern.subn(f"[REDACTED:{kind}]", text)
        count += hits
    return text, count
