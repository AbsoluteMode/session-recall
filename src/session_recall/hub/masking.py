"""Mask the secrets we already know about, by their Doppler name.

The regex scanner in `share/scanner.py` guesses at FORMATS — it catches
`sk-ant-…` because that shape is recognisable. It cannot catch
`NETCUP_PASSWORD`, because a password has no shape. This module works the
other way round: everything the team keeps in Doppler is known by value, so a
literal match is exact, has no false negatives for anything stored there, and
replaces the hit with something more useful than `[REDACTED]` — the variable
name it came from:

    ssh root@1.2.3.4 hunter2xyzzy…      ->  ssh root@1.2.3.4 ${servers/NETCUP_PASSWORD}

A reader still learns which credential the session used, which is usually the
part that matters when reading someone's history, and the value is gone.

**The hub never stores the secret values.** It keeps HMACs of them under a
local salt, and matches by hashing candidate tokens out of the incoming text.
A masking map that leaks is therefore worthless to whoever takes it — which is
the entire point of putting the team's Doppler inventory on a shared server.
The salt makes precomputation useless too, so short low-entropy values are the
only weak case, and those are excluded anyway (below).

Two deliberate limits, both of which the regex layer still covers:

- **Values that do not survive tokenisation** — connection strings, URLs with
  inline credentials, multi-line PEM blocks — never appear as one token, so
  they are not matched here. `scanner.py` recognises those by shape.
- **Short or low-entropy values are skipped.** Doppler holds `DOPPLER_CONFIG=dev`
  next to real credentials; masking every value blindly would delete the word
  "dev" from every transcript the team owns. The bar is length plus some
  variety, and names known to be non-secret are excluded outright.
"""

import hashlib
import json
import re
import secrets as pysecrets
import subprocess
import time
from pathlib import Path

from .. import perms

MIN_LENGTH = 12
_DOPPLER_TIMEOUT_S = 30

# Doppler injects these into every config; they are labels, not credentials.
_NEVER_MASK = {
    "DOPPLER_PROJECT", "DOPPLER_CONFIG", "DOPPLER_ENVIRONMENT",
}

# Only variables whose NAME says "credential" are masked. Learned the hard way
# on the first real corpus: shape alone cannot tell `claude-opus-4` from a
# token — both are short, mixed-class strings — so an entropy filter masked
# 851 mentions of a model name, 2429 of a hostname and 1536 of a server IP,
# which broke reading and searching the team's history far worse than the
# leak it prevented. A Doppler config holds credentials AND plain
# configuration; only the first kind belongs here.
_SECRET_NAME_RE = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL|PRIVATE|SIGNING|"
    r"API[_-]?KEY|ACCESS[_-]?KEY|[_-]KEY$|^KEY$|DSN|SALT|SESSION[_-]?ID",
    re.IGNORECASE)

# Three alphabets, widest first. One tokenisation is not enough: with only the
# wide class, `--key=sk-ant-AAAA…` is a single token and never equals the
# stored value, while the medium class splits it at `=` and yields the key
# itself. Cheap enough to run all three — a handful of regex passes over text
# that is about to be embedded anyway.
_ALPHABETS = (
    re.compile(rf"[A-Za-z0-9_\-+/=~.]{{{MIN_LENGTH},}}"),
    re.compile(rf"[A-Za-z0-9_\-]{{{MIN_LENGTH},}}"),
    re.compile(rf"[A-Za-z0-9]{{{MIN_LENGTH},}}"),
)
_TRIM = "-._=+/~"

# Transcripts are JSON, so a newline inside a string is the two characters
# `\` and `n`. The backslash is not in any alphabet above and ends a token,
# but the `n` IS, which glues it to whatever follows: a secret written right
# after a line break tokenises as `n<secret>` and matches nothing. Found in
# production — one real API key survived masking this way, in a message that
# read "вот замени в doppler…\n\n<key>". Same for \t, \r, \b, \f.
_JSON_ESCAPE_LETTERS = "ntrbf"


def _candidates(token: str, text: str, start: int) -> list[str]:
    """Forms of `token` worth testing against the secret map.

    Extra candidates are free: matching is by exact hash, so a form that is
    not a secret simply misses. Missing a form, by contrast, leaks.
    """
    forms = [token, token.strip(_TRIM)]
    if start > 0 and text[start - 1] == "\\" and token[0] in _JSON_ESCAPE_LETTERS:
        forms.append(token[1:])
        forms.append(token[1:].strip(_TRIM))
    return [f for f in forms if len(f) >= MIN_LENGTH]


def maskable(name: str, value: str) -> bool:
    """Is this Doppler entry a credential worth masking on sight?

    Two gates, and the NAME gate is the important one: a Doppler config mixes
    credentials with ordinary configuration (model names, hostnames, regions),
    and only the former should ever be rewritten. The shape gate then rejects
    the short, low-entropy values that would eat ordinary words.
    """
    if name in _NEVER_MASK or not value or len(value) < MIN_LENGTH:
        return False
    if not _SECRET_NAME_RE.search(name):
        return False
    if any(ch.isspace() for ch in value):
        return False          # multi-line/PEM: never a single token anyway
    classes = sum((
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() for c in value),
    ))
    # Two character classes is the ordinary credential. A single-class value
    # has to be long instead: 20+ characters with no whitespace, matching a
    # Doppler value exactly, is a generated passphrase and not prose.
    return classes >= 2 or len(value) >= 20


class SecretMap:
    """Hashed secret values -> the variable name to show instead.

    Persisted as JSON so a hub restart (or a Doppler outage) keeps masking
    with the last known inventory rather than silently starting to store
    credentials in the clear.
    """

    def __init__(self, salt: str, labels: dict[str, str], updated: int = 0):
        self.salt = salt
        self.labels = labels          # hmac hex -> "project/VAR"
        self.updated = updated

    @staticmethod
    def _digest(salt: str, token: str) -> str:
        return hashlib.blake2b(token.encode(), key=salt.encode()[:64],
                               digest_size=16).hexdigest()

    @classmethod
    def build(cls, entries: dict[str, str], salt: str | None = None) -> "SecretMap":
        """`entries` maps "project/VAR" -> value. Values are hashed and dropped."""
        salt = salt or pysecrets.token_hex(16)
        labels = {cls._digest(salt, value): name
                  for name, value in entries.items()
                  if maskable(name.split("/")[-1], value)}
        return cls(salt, labels, int(time.time()))

    @classmethod
    def load(cls, path: Path) -> "SecretMap":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(salt="", labels={})
        return cls(data.get("salt", ""), data.get("labels", {}),
                   int(data.get("updated", 0)))

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"salt": self.salt, "labels": self.labels, "updated": self.updated},
            indent=2, sort_keys=True), encoding="utf-8")
        perms.protect(tmp)
        tmp.replace(path)

    def __bool__(self) -> bool:
        return bool(self.salt and self.labels)

    def mask(self, text: str) -> tuple[str, int]:
        """Replace every known secret with `${project/VAR}`.

        Returns the masked text and how many replacements were made — the
        count is what an operator watches to know the map is actually live.
        """
        if not self:
            return text, 0
        hits = 0
        for pattern in _ALPHABETS:
            def swap(match: re.Match) -> str:
                nonlocal hits
                token = match.group(0)
                for candidate in _candidates(token, match.string, match.start()):
                    label = self.labels.get(self._digest(self.salt, candidate))
                    if label:
                        hits += 1
                        return token.replace(candidate, "${" + label + "}")
                return token
            text = pattern.sub(swap, text)
        return text, hits


def collect_from_doppler(runner=None) -> dict[str, str]:
    """Every secret the caller's Doppler token can read, as "project/VAR".

    Shells out to the `doppler` CLI rather than the API: the CLI already holds
    the machine's credentials, so the hub never needs a Doppler token of its
    own in its config.
    """
    def run(args: list[str]) -> str:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=_DOPPLER_TIMEOUT_S)
        if done.returncode != 0:
            raise RuntimeError((done.stderr or "doppler failed").strip()[:200])
        return done.stdout

    runner = runner or run
    entries: dict[str, str] = {}
    projects = json.loads(runner(["doppler", "projects", "--json"]))
    for project in projects:
        name = project.get("id") or project.get("name")
        if not name:
            continue
        try:
            configs = json.loads(
                runner(["doppler", "configs", "--json", "-p", name]))
        except (RuntimeError, ValueError):
            continue          # a project this token cannot read is not an error
        for config in configs:
            config_name = config.get("name")
            if not config_name:
                continue
            try:
                values = json.loads(runner([
                    "doppler", "secrets", "download", "--no-file",
                    "--format", "json", "-p", name, "-c", config_name]))
            except (RuntimeError, ValueError):
                continue
            for var, value in values.items():
                if isinstance(value, str):
                    entries[f"{name}/{var}"] = value
    return entries
