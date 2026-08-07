"""What "private" means on each platform, and what it costs to get it wrong.

The Windows branch is pure path logic and runs everywhere; the POSIX branch
needs a filesystem that actually has mode bits, so it runs where those exist.
"""

import os
import stat
import sys

import pytest

from session_recall.perms import exposure, protect

POSIX_ONLY = pytest.mark.skipif(sys.platform.startswith("win"),
                                reason="no mode bits on this filesystem")


@pytest.fixture
def secret(tmp_path):
    path = tmp_path / "hub.json"
    path.write_text('{"key": "sr_egor_deadbeef"}', encoding="utf-8")
    return path


@POSIX_ONLY
def test_protect_makes_the_mode_owner_only(secret):
    os.chmod(secret, 0o644)
    protect(secret)
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert exposure(secret) is None


@POSIX_ONLY
def test_a_readable_mode_is_reported_with_the_mode_in_it(secret):
    os.chmod(secret, 0o644)
    why = exposure(secret)
    assert why and "0644" in why


def test_protect_does_not_touch_the_mode_on_windows(secret):
    """`os.chmod(path, 0o600)` on Windows sets the read-only flag: it stops the
    OWNER writing and stops nobody reading. Doing nothing is the honest move,
    and a regression here would be silent — the call would look like it worked."""
    before = secret.stat().st_mode
    protect(secret, platform="win32")
    assert secret.stat().st_mode == before
    with open(secret, "a", encoding="utf-8") as fh:      # still writable
        fh.write("")


def test_windows_file_under_the_profile_is_private(secret, tmp_path):
    assert exposure(secret, private_root=tmp_path, platform="win32") is None


def test_windows_file_outside_the_profile_is_reported(secret, tmp_path):
    """The case this whole check exists for: XDG_DATA_HOME pointed at a share
    or a synced folder, where the key inherits that location's ACL."""
    why = exposure(secret, private_root=tmp_path / "elsewhere", platform="win32")
    assert why and "outside" in why


def test_missing_private_root_does_not_crash_the_check(secret, tmp_path):
    """`private_root` may not exist yet; resolving it must not raise, or
    `health` would die on the machine that most needs to hear the answer."""
    assert exposure(secret, private_root=tmp_path / "nope" / "deeper",
                    platform="win32")
