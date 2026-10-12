"""One indexer run at a time. A run can take hours against a freshly uploaded
backlog, so the timer WILL fire into a manual run sooner or later — and two
runs would fight over the same per-file transactions. flock, not a pid file:
the lock dies with the process, so a crash never wedges the job."""

import fcntl
import os
from pathlib import Path


def acquire_lock(data_dir: Path, name: str) -> int | None:
    """Returns the fd holding the lock, or None when another run owns it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / name, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd
