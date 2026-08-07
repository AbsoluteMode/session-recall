"""Client against a real hub: the two halves of the protocol only matter
together, so these run the actual server on an ephemeral port."""

import json
import stat
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from session_recall.hub import storage
from session_recall.hub.app import Hub, make_handler
from session_recall.hub.client import (CONSENT, HubConfig, HubError, join,
                                       local_files, push)
from session_recall.hub.masking import SecretMap
from session_recall.perms import exposure

ANTHROPIC = "sk-ant-api03-" + "B" * 40
NETCUP = "Xk39dmPQ7wLz2vRt"


@pytest.fixture
def hub(tmp_path):
    return Hub(tmp_path / "hub", recall_factory=lambda: None)


@pytest.fixture
def url(hub):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(hub))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def roots(tmp_path):
    """A miniature version of the three local transcript roots.

    Written as real transcripts are — utf-8, LF, no locale in the loop — so the
    byte counts these tests assert on mean the same thing on every platform."""
    def transcript(path: Path, line: str) -> None:
        path.write_text(line, encoding="utf-8", newline="")

    claude = tmp_path / "claude-projects" / "-Users-egor-proj"
    claude.mkdir(parents=True)
    transcript(claude / "sess-a.jsonl", '{"type":"user","text":"привет"}\n')
    codex = tmp_path / "codex-sessions" / "2026" / "08" / "05"
    codex.mkdir(parents=True)
    transcript(codex / "roll-1.jsonl", '{"type":"message","text":"codex"}\n')
    archive = tmp_path / "codex-archive"
    archive.mkdir()
    transcript(archive / "old.jsonl", '{"type":"message","text":"old"}\n')
    return {"claude_root": tmp_path / "claude-projects",
            "codex_sessions": tmp_path / "codex-sessions",
            "codex_archive": archive}


@pytest.fixture
def cfg(url, hub, tmp_path):
    key = hub.keys.issue("egor")
    return join(url, key, path=tmp_path / "hub.json")


def test_local_files_covers_claude_codex_and_the_archive(roots):
    rels = dict(local_files(**roots))
    assert set(rels) == {
        "claude/-Users-egor-proj/sess-a.jsonl",
        "codex/2026/08/05/roll-1.jsonl",
        "codex/archived/old.jsonl",
    }


def test_join_verifies_the_key_before_saving(url, hub, tmp_path):
    path = tmp_path / "hub.json"
    with pytest.raises(HubError, match="revoked|rejected"):
        join(url, "sr_egor_" + "0" * 32, path=path)
    assert not path.exists()          # a failed join leaves nothing behind


def test_join_stores_the_key_readable_only_by_its_owner(url, hub, tmp_path):
    """Asserted through `perms.exposure` rather than the mode directly, because
    the mechanism differs: POSIX makes the file 0600, Windows has no per-file
    mode and relies on the profile directory's ACL. The property — nobody else
    can read the key — is the same on both, so the test is too."""
    path = tmp_path / "hub.json"
    join(url, hub.keys.issue("egor"), path=path)
    assert exposure(path, private_root=tmp_path) is None
    if not sys.platform.startswith("win"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert HubConfig.load(path).url == url


def test_consent_text_states_what_actually_happens():
    assert "ВСЕМ участникам" in CONSENT
    assert "hub leave" in CONSENT


def test_push_uploads_everything_then_nothing(cfg, hub, roots):
    first = push(cfg, roots=roots)
    assert first["files"] == 3 and first["uploaded_bytes"] > 0
    assert push(cfg, roots=roots) == {
        "files": 0, "uploaded_bytes": 0, "redacted": 0,
        "skipped": 0, "failed": 0}


def test_push_sends_only_the_tail_of_a_grown_transcript(cfg, hub, roots):
    push(cfg, roots=roots)
    grown = roots["claude_root"] / "-Users-egor-proj" / "sess-a.jsonl"
    addition = '{"type":"assistant","text":"ответ"}\n'
    # explicit utf-8 and no newline translation: a transcript is counted in
    # BYTES here, and Windows text mode would silently spend a locale codepage
    # and an extra \r on every line, so the assertions below would measure the
    # test's own encoding rather than what push sent
    with open(grown, "a", encoding="utf-8", newline="") as fh:
        fh.write(addition)

    stats = push(cfg, roots=roots)
    assert stats["files"] == 1
    assert stats["uploaded_bytes"] == len(addition.encode())
    stored = storage.resolve(hub.transcripts, "egor",
                             "claude/-Users-egor-proj/sess-a.jsonl")
    assert stored.read_text(encoding="utf-8").endswith(addition)


def test_a_rewritten_transcript_is_resent_whole(cfg, hub, roots):
    push(cfg, roots=roots)
    rewritten = roots["claude_root"] / "-Users-egor-proj" / "sess-a.jsonl"
    rewritten.write_text('{"short":1}\n', encoding="utf-8", newline="")

    push(cfg, roots=roots)
    stored = storage.resolve(hub.transcripts, "egor",
                             "claude/-Users-egor-proj/sess-a.jsonl")
    assert stored.read_text(encoding="utf-8") == '{"short":1}\n'


def test_format_shaped_secrets_never_leave_the_machine(cfg, hub, roots):
    """Client-side redaction is the first layer: an API key is cut before the
    request is built, so it is not merely masked on arrival — it never travels."""
    leaky = roots["claude_root"] / "-Users-egor-proj" / "sess-a.jsonl"
    leaky.write_text(json.dumps({"text": f"key is {ANTHROPIC}"}) + "\n",
                     encoding="utf-8", newline="")

    stats = push(cfg, roots=roots)
    stored = storage.resolve(
        hub.transcripts, "egor",
        "claude/-Users-egor-proj/sess-a.jsonl").read_text(encoding="utf-8")
    assert stats["redacted"] == 1
    assert ANTHROPIC not in stored and "[REDACTED:anthropic-key]" in stored


def test_redaction_keeps_the_resume_point_aligned(cfg, hub, roots):
    """Redaction changes length, so the hub must count the CLIENT's bytes —
    otherwise the next push re-sends from a wrong offset forever."""
    leaky = roots["claude_root"] / "-Users-egor-proj" / "sess-a.jsonl"
    line = json.dumps({"text": f"key is {ANTHROPIC}"}) + "\n"
    leaky.write_text(line, encoding="utf-8", newline="")
    push(cfg, roots=roots)

    rel = "claude/-Users-egor-proj/sess-a.jsonl"
    assert hub.ledger.read("egor")[rel] == len(line.encode())
    assert push(cfg, roots=roots)["files"] == 0        # nothing left to send


def test_hub_side_masking_still_applies_to_what_the_client_missed(
        cfg, hub, roots):
    """A password has no format, so the client cannot catch it; the Doppler
    map on the hub is what stops it."""
    SecretMap.build({"servers/NETCUP_PASSWORD": NETCUP},
                    salt="test-salt").save(hub.secrets_path)
    leaky = roots["claude_root"] / "-Users-egor-proj" / "sess-a.jsonl"
    leaky.write_text(json.dumps({"text": f"sshpass -p {NETCUP}"}) + "\n")

    stats = push(cfg, roots=roots)
    stored = storage.resolve(hub.transcripts, "egor",
                             "claude/-Users-egor-proj/sess-a.jsonl").read_text()
    assert stats["redacted"] == 0                     # the client saw nothing
    assert NETCUP not in stored
    assert "${servers/NETCUP_PASSWORD}" in stored


def test_unsupported_names_are_skipped_not_fatal(cfg, hub, roots):
    odd = roots["claude_root"] / "project with spaces"
    odd.mkdir()
    (odd / "sess.jsonl").write_text("{}\n")
    stats = push(cfg, roots=roots)
    assert stats["skipped"] == 1 and stats["files"] == 3


def test_a_revoked_key_stops_the_push_with_a_clear_message(cfg, hub, roots):
    hub.keys.revoke("egor")
    with pytest.raises(HubError, match="revoked"):
        push(cfg, roots=roots)


def test_broad_assignment_pattern_does_not_eat_ordinary_text(cfg, hub, roots):
    """`token: abc12345` appears in code, JSON and prose everywhere. Cutting it
    automatically removed 27324 fragments of real work on the first corpus."""
    ordinary = roots["claude_root"] / "-Users-egor-proj" / "sess-a.jsonl"
    ordinary.write_text(json.dumps(
        {"text": "в конфиге поле token: abcdef123456 — разберись почему падает"}) + "\n")

    stats = push(cfg, roots=roots)
    stored = storage.resolve(hub.transcripts, "egor",
                             "claude/-Users-egor-proj/sess-a.jsonl").read_text()
    assert stats["redacted"] == 0
    assert "token: abcdef123456" in stored
