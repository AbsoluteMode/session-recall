"""One run at a time. Runs are hours-long against a backlog, so the nightly
launchd job WILL overlap a manual run sooner or later — and two runs would
race each other over watermarks and the git repo. A kernel lock, not a pid
file: it dies with the process, so a crash never wedges the job.

Two backends, one contract. POSIX gets `flock`; Windows has no `fcntl` at all,
so it gets `msvcrt.locking`, whose byte-range locks carry the property this
module actually depends on — a second handle onto the same file is refused
even inside the same process, and the lock is released when the process dies.
The import is guarded rather than branched on `sys.platform` so that a
platform without either module fails loudly here, at the one place that knows
what the lock is for, instead of at the first overlapping run."""

import os
from pathlib import Path

try:
    import fcntl
    msvcrt = None
except ModuleNotFoundError:            # Windows
    fcntl = None
    import msvcrt

_LOCK_BYTES = 1   # msvcrt locks a range; one byte at offset 0 is the whole point


def acquire_lock(data_dir: Path, name: str = "metadocs.lock") -> int | None:
    """Returns the fd holding the lock, or None when another run owns it.

    `name` lets a second long-running job (the hub indexer) take its own lock
    with the same semantics instead of copying this file."""
    data_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(data_dir / name, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            # LK_NBLCK fails immediately instead of retrying, matching LOCK_NB.
            # Locking past EOF is legal, so the empty lock file needs no bytes.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_BYTES)
    except OSError:
        os.close(fd)
        return None
    return fd


def release_lock(fd: int | None) -> None:
    """Give the lock back. Closing the fd is enough on both backends — this
    exists so callers holding a lock across a long run have one obvious way to
    drop it early, and so Windows unlocks the range before the handle goes."""
    if fd is None:
        return
    if msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTES)
        except OSError:
            pass
    os.close(fd)
