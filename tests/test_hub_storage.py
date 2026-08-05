"""Ingest-side storage: paths arrive over the network, so `resolve` is a
security boundary and gets adversarial tests, not happy-path ones."""

import pytest

from session_recall.hub import storage
from session_recall.hub.storage import Ledger, OffsetMismatch, UnsafePath

GOOD = "claude/-Users-egor-proj/1234-abcd.jsonl"


@pytest.fixture
def root(tmp_path):
    return tmp_path / "transcripts"


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "state")


def test_resolve_accepts_a_normal_transcript(root):
    path = storage.resolve(root, "egor", GOOD)
    assert path == root / "egor" / "claude" / "-Users-egor-proj" / "1234-abcd.jsonl"


@pytest.mark.parametrize("rel", [
    "claude/../../etc/passwd.jsonl",         # classic traversal
    "../maxim/claude/x.jsonl",               # into another member's tree
    "/etc/shadow.jsonl",                     # absolute
    "claude\\..\\x.jsonl",                   # windows-style separator
    "claude/x.jsonl\0.txt",                  # NUL truncation
    "claude/.ssh/id_rsa.jsonl",              # dot-leading segment
    "secrets/x.jsonl",                       # unknown source root
    "claude/proj/notes.txt",                 # not a transcript
    "claude",                                # too shallow
    "claude/" + "a/" * 10 + "x.jsonl",       # too deep
])
def test_resolve_rejects_hostile_paths(root, rel):
    with pytest.raises(UnsafePath):
        storage.resolve(root, "egor", rel)


def test_resolve_rejects_bad_owner(root):
    with pytest.raises(UnsafePath):
        storage.resolve(root, "../maxim", GOOD)


def test_resolve_refuses_to_follow_a_planted_symlink(root, tmp_path):
    """Pattern matching cannot see a symlink, so the resolved path is checked
    against the resolved owner root as an independent second gate."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(parents=True)
    (root / "egor").mkdir(parents=True)
    (root / "egor" / "claude").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(UnsafePath):
        storage.resolve(root, "egor", GOOD)


def test_append_then_extend(root, ledger):
    assert storage.append(root, "egor", GOOD, 0, b"line1\n", ledger) == 6
    assert storage.append(root, "egor", GOOD, 6, b"line2\n", ledger) == 12
    assert storage.resolve(root, "egor", GOOD).read_bytes() == b"line1\nline2\n"


def test_append_at_wrong_offset_reports_what_we_have(root, ledger):
    storage.append(root, "egor", GOOD, 0, b"line1\n", ledger)
    with pytest.raises(OffsetMismatch) as raised:
        storage.append(root, "egor", GOOD, 99, b"late\n", ledger)
    assert raised.value.actual == 6


def test_offset_zero_replaces_a_rewritten_transcript(root, ledger):
    storage.append(root, "egor", GOOD, 0, b"old content\n", ledger)
    assert storage.append(root, "egor", GOOD, 0, b"new\n", ledger) == 4
    assert storage.resolve(root, "egor", GOOD).read_bytes() == b"new\n"


def test_ledger_counts_client_bytes_not_stored_bytes(root, ledger):
    """Masking rewrites the text, so the manifest must track how much of the
    CLIENT's file we consumed — otherwise every masked transcript looks
    truncated and uploads forever."""
    storage.append(root, "egor", GOOD, 0, b"short\n", ledger, received_len=500)
    assert storage.manifest(root, "egor", ledger) == {GOOD: 500}


def test_manifest_drops_entries_whose_file_is_gone(root, ledger):
    storage.append(root, "egor", GOOD, 0, b"x\n", ledger)
    storage.resolve(root, "egor", GOOD).unlink()
    assert storage.manifest(root, "egor", ledger) == {}


def test_manifest_is_per_owner(root, ledger):
    storage.append(root, "egor", GOOD, 0, b"x\n", ledger)
    assert storage.manifest(root, "maxim", ledger) == {}


def test_owners_lists_only_valid_member_directories(root):
    for name in ("egor", "maxim"):
        (root / name).mkdir(parents=True)
    (root / "NOT A NAME").mkdir()
    (root / "loose.jsonl").parent.mkdir(exist_ok=True)
    (root / "loose.jsonl").write_text("")
    assert storage.owners(root) == ["egor", "maxim"]
