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

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
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
