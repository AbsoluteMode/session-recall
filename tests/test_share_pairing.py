import sys
import time

import pytest

from session_recall.perms import exposure
from session_recall.share import identity as identity_mod
from session_recall.share import pairing
from session_recall.share.pairing import PairingError
from session_recall.share.transport import InMemoryTransport


@pytest.fixture
def two_sides(tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    return (identity_mod.create(a_dir, "maxim"), a_dir,
            identity_mod.create(b_dir, "egor"), b_dir,
            InMemoryTransport())


def test_happy_path_same_sas_both_sides(two_sides):
    a, a_dir, b, b_dir, t = two_sides
    code = pairing.start_invite(a, t, a_dir)
    joined = pairing.join(b, t, b_dir, code)
    completed = pairing.complete_invite(a, t, a_dir)

    assert joined.sas == completed.sas
    assert joined.bundle["name"] == "maxim"
    assert completed.bundle["name"] == "egor"
    assert joined.bundle["sign_pk"] == a.sign_pk_b64
    # both sides parked a pending peer for the explicit `trust` step
    assert pairing.pending_peer(a_dir)["bundle"]["name"] == "egor"
    assert pairing.pending_peer(b_dir)["bundle"]["name"] == "maxim"


def test_wrong_code_rejected(two_sides):
    a, a_dir, b, b_dir, t = two_sides
    code = pairing.start_invite(a, t, a_dir)
    # same slot id, different key: MAC must fail, not decode garbage
    tampered = code[:-4] + ("aaaa" if not code.endswith("aaaa") else "bbbb")
    with pytest.raises(PairingError):
        pairing.join(b, t, b_dir, tampered)


def test_unknown_invite_id(two_sides):
    _, _, b, b_dir, t = two_sides
    with pytest.raises(PairingError, match="not found"):
        pairing.join(b, t, b_dir, "a" * 60)  # well-formed code, nonexistent slot


def test_garbage_code_rejected(two_sides):
    _, _, b, b_dir, t = two_sides
    with pytest.raises(PairingError):
        pairing.join(b, t, b_dir, "not-a-code")


def test_expired_invite(two_sides, monkeypatch):
    a, a_dir, b, b_dir, t = two_sides
    code = pairing.start_invite(a, t, a_dir)
    real = time.time
    monkeypatch.setattr(pairing.time, "time", lambda: real() + pairing.INVITE_TTL_S + 5)
    with pytest.raises(PairingError, match="expired"):
        pairing.join(b, t, b_dir, code)


def test_complete_before_join(two_sides):
    a, a_dir, _, _, t = two_sides
    pairing.start_invite(a, t, a_dir)
    with pytest.raises(PairingError, match="not joined yet"):
        pairing.complete_invite(a, t, a_dir)


def test_complete_without_invite(two_sides):
    a, a_dir, _, _, t = two_sides
    with pytest.raises(PairingError, match="no pending invite"):
        pairing.complete_invite(a, t, a_dir)


def test_identity_files_are_private(two_sides):
    """Same property on both platforms, different mechanism underneath — see
    `perms.exposure`: mode bits where they exist, the profile directory's ACL
    where they do not."""
    _, a_dir, _, _, _ = two_sides
    identity = a_dir / "identity.json"
    assert exposure(identity, private_root=a_dir) is None
    if not sys.platform.startswith("win"):
        assert identity.stat().st_mode & 0o077 == 0, "identity must be 0600"


def test_identity_create_refuses_overwrite(tmp_path):
    identity_mod.create(tmp_path, "maxim")
    with pytest.raises(FileExistsError):
        identity_mod.create(tmp_path, "maxim-again")
