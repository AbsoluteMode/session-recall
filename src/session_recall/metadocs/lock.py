"""One run at a time. Runs are hours-long against a backlog, so the nightly
launchd job WILL overlap a manual run sooner or later — and two runs would
race each other over watermarks and the git repo. flock, not a pid file: the
lock dies with the process, so a crash never wedges the job."""

import fcntl
import os
from pathlib import Path


def acquire_lock(data_dir: Path, name: str = "metadocs.lock") -> int | None:
    """Returns the fd holding the lock, or None when another run owns it.

    `name` lets a second long-running job (the hub indexer) take its own lock
    with the same semantics instead of copying this file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / name, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd
