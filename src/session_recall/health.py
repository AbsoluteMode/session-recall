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


def check_freshness(store: Store, transcripts: list[Path]) -> Dimension:
    """Gap between the newest transcript on disk and the newest turn in the index.

    Deliberately not "when did the index last change": an indexer that runs every
    session and fails every time keeps its own timestamp fresh while falling further
    and further behind reality. Only the comparison against disk catches that.
    """
    newest_indexed = store.db.execute("SELECT MAX(ts) FROM chunks").fetchone()[0] or 0
    on_disk = [p.stat().st_mtime for p in transcripts if p.exists()]
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
                     "set CODEX_HOME / SESSION_RECALL_CLAUDE_PROJECTS if these live elsewhere")


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


@dataclass(frozen=True)
class Report:
    dimensions: list[Dimension]
    verdict: str


def check_all(store: Store, embedder, roots: dict[str, Path],
              transcripts: list[Path]) -> Report:
    """Every dimension plus one verdict. The verdict is the worst zone present: a
    single dead dimension makes recall untrustworthy, and averaging would hide it
    behind everything that still works."""
    dims = [
        check_freshness(store, transcripts),
        check_embedder(embedder),
        check_corpus(store),
        check_paths(roots),
    ]
    order = {"GREEN": 0, "AMBER": 1, "RED": 2}
    verdict = max((d.zone for d in dims), key=lambda z: order[z])
    return Report(dimensions=dims, verdict=verdict)
