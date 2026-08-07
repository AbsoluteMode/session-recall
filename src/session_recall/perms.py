"""Who else can read a file that holds a secret.

Two platforms, two mechanisms, and they are not interchangeable.

POSIX has per-file mode bits and NEEDS them: the data directory is 0755, so a
key written with the default mode is readable by every account on the box.
0600 is what makes it private, which is why the writers here create the file
with that mode rather than fixing it afterwards.

Windows has no mode bits — `os.chmod` there moves the read-only flag and
nothing else — and does not need them for the same reason: a user profile
directory already carries a DACL that no other unprivileged account can
traverse. Hand-building a DACL on top (`icacls /inheritance:r /grant:r`) would
mostly restate the inherited one, and the single account it could additionally
exclude is an administrator, who can take ownership of the file anyway. It
would also have to name accounts on a localised system and would fail on a
share — cost and failure modes for no property gained.

So `protect` sets the mode where the mode is the mechanism, and `exposure`
checks the property that actually holds on each platform instead of the one
POSIX happens to name. That check is the part worth having: it catches the
case that really does leak on Windows — a data directory pointed outside the
profile (`XDG_DATA_HOME` on a share, a synced folder), where the file inherits
whatever that location grants to whoever.
"""

import os
import stat
import sys
from pathlib import Path

SECRET_MODE = 0o600


def _mode_bits_are_the_mechanism(platform: str) -> bool:
    return not platform.startswith("win")


def protect(path: Path, platform: str | None = None) -> None:
    """Make `path` private, where that is something a program can do.

    A no-op on Windows on purpose: `os.chmod(path, 0o600)` there sets the
    read-only flag, which stops the OWNER from writing and stops nobody from
    reading — the opposite of the intent, dressed as the intent."""
    if _mode_bits_are_the_mechanism(platform or sys.platform):
        os.chmod(path, SECRET_MODE)


def exposure(path: Path, private_root: Path | None = None,
             platform: str | None = None) -> str | None:
    """Why `path` is readable beyond its owner, or None when it is not.

    `private_root` is the directory whose ACL is trusted to be per-user — the
    home directory in production, injectable so tests do not have to write a
    fake key into the real profile to exercise the Windows branch."""
    path = Path(path)
    platform = platform or sys.platform
    if _mode_bits_are_the_mechanism(platform):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            return f"mode {mode:04o} — group or other can read it"
        return None
    root = Path(private_root if private_root is not None else Path.home())
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        # Also the UNC case: a \\server\share path is never under the profile,
        # and its ACL is the file server's business, not ours.
        return (f"outside {root} — Windows has no per-file mode bits, so it "
                f"inherits whatever that location grants")
    return None
