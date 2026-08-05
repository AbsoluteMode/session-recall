"""Where a hub keeps what clients send, and the boundary that keeps it safe.

One tree per member, mirroring the roots each client has locally:

    <root>/transcripts/<owner>/claude/<project-dir>/<session>.jsonl
    <root>/transcripts/<owner>/codex/<yyyy>/<mm>/<dd>/<rollout>.jsonl

That mirroring is load-bearing, not cosmetic: `index.index_corpus` walks
exactly these shapes, so a hub indexes a colleague's history through the same
code that indexes a laptop's own.

Every segment of a stored path arrives over the network, which makes `resolve`
the security boundary of the ingest side. Three things it must survive, all of
them cheap to get wrong:

- `..` and absolute paths escaping into another member's tree (or out of the
  data directory entirely);
- names that are not path traversal but still hostile to the filesystem —
  empty, dot-only, control characters, a leading dash;
- a symlink planted by an earlier write redirecting a later one. Pattern
  matching alone cannot see that, so the resolved path is re-checked against
  the resolved owner root.

Transcripts are append-only in normal operation, so the wire protocol is
offset-based: a client says "at byte N I have these bytes". A mismatch is
reported rather than papered over — silently appending at the wrong offset
would interleave two writers into a file that no longer parses.

**Received bytes are counted separately from file size.** Secret masking
rewrites the incoming text, so what lands on disk is a different length from
what the client sent. The ledger therefore records how much of the CLIENT's
file we have consumed; sizing the manifest off the stored file instead would
make every masked transcript look truncated and re-upload forever.
"""

import json
import os
import re
from pathlib import Path

# One segment of a relative path. Deliberately narrow: transcript names are
# session ids, project directories and date parts, none of which need more.
# The first character cannot be a dot, which rules out `..` and hidden files.
# A LEADING DASH IS ALLOWED and must stay allowed: Claude Code names its
# project directories after the encoded cwd (`-Users-egor-proj`), so rejecting
# them would reject every Claude transcript there is. Nothing here reaches a
# shell or an argv, so a flag-shaped name is inert.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}$")
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# The only roots a client may write into. `source` names match the indexer's.
SOURCES = ("claude", "codex")

MAX_DEPTH = 8


class UnsafePath(ValueError):
    """A relative path from a client that will not be written anywhere."""


class OffsetMismatch(ValueError):
    """The client's idea of the file length disagrees with ours."""

    def __init__(self, actual: int):
        super().__init__(f"file is {actual} bytes")
        self.actual = actual


def valid_owner(owner: str) -> bool:
    return bool(_OWNER_RE.match(owner or ""))


def owner_root(root: Path, owner: str) -> Path:
    if not valid_owner(owner):
        raise UnsafePath(f"bad owner name: {owner!r}")
    return Path(root) / owner


def resolve(root: Path, owner: str, rel: str) -> Path:
    """Absolute path for a client-supplied relative path, or raise UnsafePath.

    `rel` must start with a known source directory and consist of plain
    segments; the result is verified to sit under the owner's root even after
    symlinks are followed.
    """
    if not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
        raise UnsafePath(f"bad path: {rel!r}")
    parts = rel.split("/")
    if len(parts) < 2 or len(parts) > MAX_DEPTH:
        raise UnsafePath(f"bad path depth: {rel!r}")
    if parts[0] not in SOURCES:
        raise UnsafePath(f"unknown source directory: {parts[0]!r}")
    for part in parts:
        if not _SEGMENT_RE.match(part):
            raise UnsafePath(f"bad path segment: {part!r}")
    if not parts[-1].endswith(".jsonl"):
        raise UnsafePath(f"not a transcript: {parts[-1]!r}")

    base = owner_root(root, owner)
    target = base.joinpath(*parts)
    # Second, independent check: the pattern above cannot see a symlink that an
    # earlier write left behind, so compare the resolved paths. strict=False —
    # the file legitimately does not exist yet on a first upload.
    resolved_base = base.resolve(strict=False)
    if not target.resolve(strict=False).is_relative_to(resolved_base):
        raise UnsafePath(f"path escapes its owner root: {rel!r}")
    return target


def is_safe_rel(rel: str) -> bool:
    """Would `resolve` accept this path? Used by the CLIENT before uploading.

    Shared with the server on purpose: a client that applies the same rule
    skips the handful of local directories the hub would reject (odd
    characters in a project name) with a warning, instead of discovering it as
    a 400 in the middle of a long push.
    """
    try:
        resolve(Path("/nonexistent-probe"), "probe", rel)
    except UnsafePath:
        return False
    return True


class Ledger:
    """How many bytes of each client-side file we have consumed.

    Kept outside the transcript trees (the indexer walks those) and written
    atomically, so an interrupted upload resumes from a truthful number rather
    than a half-written one.
    """

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)

    def _path(self, owner: str) -> Path:
        if not valid_owner(owner):
            raise UnsafePath(f"bad owner name: {owner!r}")
        return self.dir / f"{owner}.json"

    def read(self, owner: str) -> dict[str, int]:
        try:
            data = json.loads(self._path(owner).read_text())
        except (OSError, ValueError):
            return {}
        return {k: int(v) for k, v in data.items() if isinstance(v, int)}

    def write(self, owner: str, rel: str, received: int) -> None:
        data = self.read(owner)
        data[rel] = received
        path = self._path(owner)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        tmp.replace(path)


def manifest(root: Path, owner: str, ledger: Ledger) -> dict[str, int]:
    """Relative path -> bytes of the client's file already consumed.

    The client diffs this against its own files and uploads only the tails,
    which is what keeps a re-push of a months-long history nearly free. Entries
    whose stored transcript has since disappeared are dropped, so a deleted
    file on the hub is re-sent whole instead of being silently skipped.
    """
    base = owner_root(root, owner)
    return {rel: received for rel, received in ledger.read(owner).items()
            if (base / rel).is_file()}


def append(root: Path, owner: str, rel: str, offset: int, data: bytes,
           ledger: Ledger, received_len: int | None = None) -> int:
    """Store `data` for the client's byte range starting at `offset`.

    `received_len` is how many bytes of the CLIENT's file this covers, which
    differs from len(data) once masking has rewritten the text. Returns the new
    received total.

    offset == current received total appends (the normal case for a growing
    transcript). offset == 0 replaces the file: a client sends that when its
    local copy no longer matches what we hold — a rewritten or rotated
    transcript — and replacing is the only way to converge. Anything else is a
    real disagreement and raises.
    """
    if offset < 0:
        raise UnsafePath(f"negative offset: {offset}")
    target = resolve(root, owner, rel)
    current = ledger.read(owner).get(rel, 0) if target.exists() else 0
    if offset not in (0, current):
        raise OffsetMismatch(current)
    target.parent.mkdir(parents=True, exist_ok=True)
    # "wb" for a replacement, "ab" to extend. Not "r+b": appending through the
    # append flag is atomic against a concurrent push from the same member's
    # second machine, where a seek+write pair is not.
    with open(target, "wb" if offset == 0 else "ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    total = offset + (len(data) if received_len is None else received_len)
    ledger.write(owner, rel, total)
    return total


def owners(root: Path) -> list[str]:
    """Members that have sent anything, for the indexer to walk."""
    base = Path(root)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_dir() and not p.is_symlink() and valid_owner(p.name))


def source_roots(root: Path, owner: str) -> dict[str, Path]:
    """The per-source directories to hand to the indexer for one member."""
    base = owner_root(root, owner)
    return {source: base / source for source in SOURCES}
