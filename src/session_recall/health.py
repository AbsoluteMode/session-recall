# src/session_recall/health.py
"""Answers "is recall actually working right now?".

Written after a failure that stayed invisible for a day and a half: the embedding
provider started refusing requests, indexing silently stopped, and search kept
returning plausible keyword hits the whole time. Nothing in the tool said a word.

Each dimension is a small check returning the same shape, so `session-recall health`
is a table and not a wall of prose. Every failing dimension carries a hint — a symptom
without a next step just moves the confusion.
WHY: docs/decisions/2026-07-26-voyage-403-egress-via-netcup.md
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import config
from .store import Store


@dataclass(frozen=True)
class Dimension:
    name: str
    zone: str  # GREEN | AMBER | RED
    detail: str
    hint: str = ""


@dataclass(frozen=True)
class Score:
    zone: str
    value: float


def score(value: float, green: float, amber: float,
          higher_is_better: bool = True) -> Score:
    """Bucket a measurement. Directional because some dimensions improve as the number
    grows (sessions indexed) and others as it shrinks (hours since the last index)."""
    if higher_is_better:
        zone = "GREEN" if value >= green else "AMBER" if value >= amber else "RED"
    else:
        zone = "GREEN" if value <= green else "AMBER" if value <= amber else "RED"
    return Score(zone=zone, value=value)


def _humanize_lag(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes behind"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours behind"
    return f"{seconds / 86400:.1f} days behind"


def check_freshness(store: Store, transcripts: list[Path],
                    source_timestamps: tuple[int | float, ...] = ()) -> Dimension:
    """Gap between the newest transcript on disk and the newest turn in the index.

    Deliberately not "when did the index last change": an indexer that runs every
    session and fails every time keeps its own timestamp fresh while falling further
    and further behind reality. Only the comparison against disk catches that.
    """
    newest_indexed = store.db.execute("SELECT MAX(ts) FROM chunks").fetchone()[0] or 0
    on_disk = [p.stat().st_mtime for p in transcripts if p.exists()]
    on_disk.extend(float(ts) for ts in source_timestamps if ts > 0)
    if not on_disk:
        return Dimension("Freshness", "AMBER", "no transcripts found on disk",
                         "check the source paths below")
    lag = max(on_disk) - newest_indexed
    zone = score(lag / 3600, green=6, amber=48, higher_is_better=False).zone
    return Dimension(
        "Freshness", zone, _humanize_lag(lag) if lag > 0 else "up to date",
        "" if zone == "GREEN" else
        "run `session-recall index` and read its output — indexing is failing, not idle")


def check_corpus(store: Store) -> Dimension:
    """Sessions per engine. A single total hides the failure worth catching: one
    source quietly stopping while the other keeps the number growing."""
    per_source = dict(store.db.execute(
        "SELECT source, COUNT(DISTINCT session_id) FROM chunks GROUP BY source"))
    total = sum(per_source.values())
    zone = score(total, green=20, amber=1).zone
    detail = ", ".join(f"{src} {n}" for src, n in sorted(per_source.items())) or "empty"
    return Dimension("Corpus", zone, f"{total} sessions ({detail})",
                     "" if zone == "GREEN" else "run `session-recall index` to populate")


def check_paths(roots: dict[str, Path]) -> Dimension:
    """A mistyped CODEX_HOME indexes nothing and is indistinguishable from having no
    Codex history at all — so name the sources that are not there."""
    missing = [name for name, path in roots.items() if not Path(path).exists()]
    if not missing:
        return Dimension("Sources", "GREEN", ", ".join(sorted(roots)) + " present")
    zone = "RED" if len(missing) == len(roots) else "AMBER"
    return Dimension("Sources", zone, f"missing: {', '.join(sorted(missing))}",
                     "set CODEX_HOME / SESSION_RECALL_CLAUDE_PROJECTS / "
                     "SESSION_RECALL_CURSOR_DB if these live elsewhere")


def check_embedder(embedder) -> Dimension:
    """One real embedding call. Quote whatever comes back verbatim: a generic
    "unavailable" is what sent us chasing the API key yesterday, when the key was
    fine and an IP-level block was answering 403 to everyone."""
    started = time.monotonic()
    try:
        embedder.embed_query("health check")
    except Exception as exc:
        return Dimension(
            "Embedder", "RED", f"{type(exc).__name__}: {exc}"[:160],
            "search still runs on keyword matching only; a 403 with an HTML body is "
            "usually the network path, not the key")
    elapsed_ms = (time.monotonic() - started) * 1000
    zone = score(elapsed_ms, green=2000, amber=8000, higher_is_better=False).zone
    return Dimension("Embedder", zone, f"responded in {elapsed_ms:.0f} ms",
                     "" if zone == "GREEN" else "slow provider will make indexing crawl")


def check_embed_space(store: Store) -> Dimension:
    """The provider can be healthy while the index belongs to another model."""
    current = config.embed_fingerprint()
    stored = store.get_meta("embed_fp")
    if stored == current:
        return Dimension("Vector space", "GREEN", current)
    if stored == "mixed":
        return Dimension(
            "Vector space", "RED", "index contains mixed embedding spaces",
            "run `session-recall index` for all sources before searching")
    if stored:
        return Dimension(
            "Vector space", "RED", f"index: {stored}; configured: {current}",
            "run `session-recall index` to re-embed with the configured model")
    return Dimension(
        "Vector space", "AMBER", "legacy index has no embedding-space marker",
        "run `session-recall index` once to attest every source")


def check_secrets(secret_files: tuple[Path, ...]) -> Dimension | None:
    """Are the files holding keys actually private?

    Worth a dimension rather than a comment because the answer depends on
    where the data directory ended up, and nothing else in the tool would ever
    say so. None when this machine holds no such file yet — an empty row would
    only be noise before the first `hub join`."""
    from .perms import exposure

    present = [p for p in secret_files if Path(p).exists()]
    if not present:
        return None
    leaks = [(p, why) for p in present if (why := exposure(p))]
    if not leaks:
        return Dimension("Key files", "GREEN",
                         f"{len(present)} private to this account")
    path, why = leaks[0]
    return Dimension(
        "Key files", "RED", f"{path.name}: {why}",
        "move the data directory back under your home directory, or treat the "
        "key as shared and reissue it")


@dataclass(frozen=True)
class Report:
    dimensions: list[Dimension]
    verdict: str


def check_all(store: Store, embedder, roots: dict[str, Path],
              transcripts: list[Path],
              source_timestamps: tuple[int | float, ...] = (),
              secret_files: tuple[Path, ...] = ()) -> Report:
    """Every dimension plus one verdict. The verdict is the worst zone present: a
    single dead dimension makes recall untrustworthy, and averaging would hide it
    behind everything that still works."""
    dims = [
        check_freshness(store, transcripts, source_timestamps),
        check_embedder(embedder),
        check_embed_space(store),
        check_corpus(store),
        check_paths(roots),
    ]
    secrets = check_secrets(secret_files)
    if secrets is not None:
        dims.append(secrets)
    order = {"GREEN": 0, "AMBER": 1, "RED": 2}
    verdict = max((d.zone for d in dims), key=lambda z: order[z])
    return Report(dimensions=dims, verdict=verdict)
