"""`session-recall setup` — the one-time project onboarding.

Asks the question that steers defaults — the interaction language, which
picks the bundled embedding model — records the answer in the settings file,
shows what the environment resolves to, and offers to run the first index.
Safe to re-run: it edits settings, it never wipes anything.

The index runs as a child process on purpose: embed settings resolve at
import time, so only a fresh process sees the language this command just
stored.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import config

_PROMPT = ("Interaction language — steers the bundled embedding model "
           "[en/zh/ru/…, Enter = multilingual]: ")


def add_parser(sub) -> None:
    p = sub.add_parser(
        "setup", help="one-time onboarding: language, embedder, first index")
    p.add_argument("--lang", help="interaction language (skips the question)")
    p.add_argument("--yes", action="store_true",
                   help="non-interactive: accept defaults and run the first index")


def _store_lang(lang: str) -> None:
    """Read-modify-write: setup owns one key, whatever else lives in the
    settings file stays."""
    settings = {}
    try:
        settings = json.loads(config.SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    settings["lang"] = lang
    config.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n",
                                    encoding="utf-8")


def _transcript_footprint() -> tuple[int, int]:
    """How much history is waiting — the first index on the bundled CPU model
    is minutes-per-months, and saying so beats looking hung."""
    files, size = 0, 0
    for root in (config.CLAUDE_PROJECTS, config.CODEX_SESSIONS,
                 config.CODEX_ARCHIVED_SESSIONS):
        root = Path(root)
        if not root.is_dir():
            continue
        for p in root.rglob("*.jsonl"):
            files += 1
            try:
                size += p.stat().st_size
            except OSError:
                pass
    cursor_db = Path(config.CURSOR_DB)
    if cursor_db.is_file():
        files += 1
        for path in (cursor_db, cursor_db.with_name(cursor_db.name + "-wal")):
            try:
                size += path.stat().st_size
            except OSError:
                pass
    return files, size


def _run_index() -> int:
    return subprocess.run([sys.executable, "-m", "session_recall.cli",
                           "index"]).returncode


def run(args: argparse.Namespace, index_cmd=_run_index,
        ask=input, is_tty=None) -> int:
    interactive = (sys.stdin.isatty() if is_tty is None else is_tty) and not args.yes

    lang = (args.lang or "").strip().lower()
    if not lang and interactive:
        lang = ask(_PROMPT).strip().lower()
    if lang:
        _store_lang(lang)
        print(f"language: {lang} (stored in {config.SETTINGS_PATH})")
    else:
        print("language: not set — the multilingual bundled model covers everyone")

    s = config.resolve_embed()   # fresh & live: sees the answer just stored
    print(f"embedder: {s.provider} / {s.model} (dim {s.dim})\n"
          "  (a VOYAGE_API_KEY, a running local server, or SESSION_RECALL_EMBED "
          "override this — see README → Embedding providers)")

    files, size = _transcript_footprint()
    print(f"history on disk: {files} transcript(s), {size / 1e6:.1f} MB")

    go = args.yes or (interactive and
                      ask("run the first index now? [Y/n]: ").strip().lower()
                      not in ("n", "no"))
    if not go:
        print("when ready: session-recall index")
        return 0
    return index_cmd()
