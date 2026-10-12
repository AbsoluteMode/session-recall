import os

from session_recall.hub.lock import acquire_lock
from session_recall.hub.indexer import LOCK_NAME


def test_second_run_steps_aside_while_first_holds_lock(tmp_path):
    fd = acquire_lock(tmp_path, LOCK_NAME)
    assert fd is not None
    assert acquire_lock(tmp_path, LOCK_NAME) is None
    os.close(fd)
    fd2 = acquire_lock(tmp_path, LOCK_NAME)
    assert fd2 is not None
    os.close(fd2)
