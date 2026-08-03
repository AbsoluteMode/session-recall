"""`setup` — the onboarding must ask once, store the answer, and never be the
thing that surprises: no question when not a tty, no index without consent."""

import argparse
import json

import pytest

from session_recall import config, onboarding


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    # keep the footprint scan away from the real machine
    monkeypatch.setattr(config, "CLAUDE_PROJECTS", tmp_path / "cl")
    monkeypatch.setattr(config, "CODEX_SESSIONS", tmp_path / "cx")
    monkeypatch.setattr(config, "CODEX_ARCHIVED_SESSIONS", tmp_path / "cxa")
    monkeypatch.setattr(config, "CURSOR_DB", tmp_path / "cursor.vscdb")
    return path


def _args(lang=None, yes=False):
    return argparse.Namespace(lang=lang, yes=yes)


def test_explicit_lang_is_stored_and_merged(settings):
    settings.write_text('{"keep": "me"}')
    rc = onboarding.run(_args(lang="RU"), index_cmd=lambda: 99, is_tty=False)
    assert rc == 0, "no tty and no --yes: the index must not run"
    stored = json.loads(settings.read_text())
    assert stored == {"keep": "me", "lang": "ru"}


def test_interactive_asks_once_and_runs_the_index_on_consent(settings):
    answers = iter(["zh", "y"])
    ran = []
    rc = onboarding.run(_args(), index_cmd=lambda: ran.append(1) or 0,
                        ask=lambda prompt: next(answers), is_tty=True)
    assert rc == 0 and ran == [1]
    assert json.loads(settings.read_text())["lang"] == "zh"


def test_interactive_no_declines_the_index(settings):
    answers = iter(["", "n"])
    rc = onboarding.run(_args(), index_cmd=lambda: 99,
                        ask=lambda prompt: next(answers), is_tty=True)
    assert rc == 0
    assert not settings.exists(), "empty answer stores nothing — multi by default"


def test_yes_is_fully_non_interactive(settings):
    def never_ask(prompt):
        raise AssertionError("--yes must not prompt")
    rc = onboarding.run(_args(lang="en", yes=True), index_cmd=lambda: 7,
                        ask=never_ask, is_tty=True)
    assert rc == 7, "--yes runs the index and surfaces its exit code"
    assert json.loads(settings.read_text())["lang"] == "en"


def test_footprint_counts_transcripts(settings, tmp_path):
    d = tmp_path / "cl" / "proj"
    d.mkdir(parents=True)
    (d / "a.jsonl").write_text("x" * 100)
    (d / "b.jsonl").write_text("y" * 50)
    files, size = onboarding._transcript_footprint()
    assert files == 2 and size == 150


def test_footprint_includes_cursor_database_and_live_wal(settings, tmp_path):
    db = tmp_path / "cursor.vscdb"
    db.write_bytes(b"d" * 100)
    db.with_name("cursor.vscdb-wal").write_bytes(b"w" * 50)
    files, size = onboarding._transcript_footprint()
    assert files == 1 and size == 150
