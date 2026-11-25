"""Offline tests for the lock a compaction holds on a month. It is a lock of the
operating system on a file: a process that dies gives it up, so what a killed run leaves
behind is a file that locks nothing. A lock that cannot be taken for any reason other
than a holder is an error, not a busy month."""

import errno
import os
import subprocess
import sys
import threading
import time

import pytest

from geovault import store
from geovault.store import Writer, _MonthLock, compact_month, lock_file, month_files

RES, MONTH = 10.0, "2024-06-01"

# A process that takes the lock of the month it is given, says so, and waits for a line.
# It then exits the way a killed process ends: at once, without letting go of anything.
HOLDER = """
import os, sys
from geovault.store import _MonthLock
lock = _MonthLock(sys.argv[1], wait=False)
print("held" if lock.acquire() else "refused", os.getpid(), flush=True)
sys.stdin.readline()
os._exit(1)
"""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE", tmp_path / "store")
    return tmp_path / "store"


def open_month(parts=2):
    """A month of `parts` open part files, one tile each. Returns its lock file."""
    w = Writer("t")
    for x in range(parts):
        w.add("B", "2024-06-03", x, 0, "EPSG:32636", RES, 10, (0, 0, 1, 1), 0, None, "s",
              bytes([x]) * 1000, None)
        w.flush()
    return lock_file("t", RES, MONTH)


def free(lock) -> bool:
    """Can the month be locked right now? Takes the lock and gives it back."""
    probe = _MonthLock(lock, wait=False)
    if not probe.acquire():
        return False
    probe.release()
    return True


def failing(real, lock, error):
    """`real` (os.open, os.unlink) for every path but the lock file, which raises."""
    def call(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(lock):
            raise error
        return real(path, *args, **kwargs)
    return call


def test_a_lock_file_left_by_a_killed_run_locks_nothing(vault):
    lock = open_month()
    lock.write_text("999999")
    sealed = compact_month("t", RES, MONTH, wait=False)
    assert month_files("t", RES, MONTH) == [sealed] and not lock.exists()


def test_the_lock_goes_with_the_process_that_held_it(vault):
    lock = open_month()
    child = subprocess.Popen([sys.executable, "-c", HOLDER, str(lock)], text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    watchdog = threading.Timer(120, child.kill)     # a child that hangs must not hang the run
    watchdog.start()
    try:
        said, pid = child.stdout.readline().split()
        assert said == "held"
        assert compact_month("t", RES, MONTH, wait=False) is False      # while it lives
        assert lock.read_bytes() == pid.encode()    # readable while the lock is held
        assert len(month_files("t", RES, MONTH)) == 2
        child.stdin.write("\n")
        child.stdin.flush()
        child.wait(timeout=60)
        assert lock.exists()                        # it did not get to delete its file
        waiting = _MonthLock(lock, wait=True, timeout=60)   # the handle may close a moment later
        assert waiting.acquire()
        waiting.release()
        sealed = compact_month("t", RES, MONTH, wait=False)
        assert month_files("t", RES, MONTH) == [sealed] and not lock.exists()
    finally:
        watchdog.cancel()
        child.kill()


def test_one_holder_at_a_time_and_no_file_once_it_is_free(vault):
    lock = open_month()
    first, second = _MonthLock(lock, wait=False), _MonthLock(lock, wait=False)
    assert first.acquire() and not second.acquire()
    assert lock.read_bytes() == str(os.getpid()).encode()
    with open(lock, "rb") as f:                     # as a viewer reads it, a block at a time
        assert f.read(65536) == str(os.getpid()).encode()
    first.release()
    assert not lock.exists()
    assert second.acquire()
    second.release()
    second.release()                                # letting go twice is letting go once
    assert not lock.exists() and free(lock)


def test_a_waiting_compaction_runs_once_the_month_is_free(vault):
    lock = open_month()
    held = _MonthLock(lock, wait=False)
    assert held.acquire()
    t0 = time.time()
    threading.Timer(0.3, held.release).start()
    sealed = compact_month("t", RES, MONTH)         # wait=True
    assert time.time() - t0 >= 0.25
    assert month_files("t", RES, MONTH) == [sealed] and not lock.exists()


def test_many_threads_never_hold_the_month_together(vault):
    lock = open_month()
    inside, most, turns, errors = [0], [0], [0], []
    count = threading.Lock()

    def worker():
        try:
            for _ in range(20):
                mine = _MonthLock(lock, wait=True, timeout=60)
                if not mine.acquire():
                    raise TimeoutError("the lock was not free within a minute")
                with count:
                    inside[0] += 1
                    most[0] = max(most[0], inside[0])
                    turns[0] += 1
                time.sleep(0.001)
                with count:
                    inside[0] -= 1
                mine.release()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(180)
    assert errors == [] and turns[0] == 120 and most[0] == 1
    assert free(lock)


# --- a lock on a file that is no longer the lock file -------------------------------------

def test_a_lock_on_a_file_that_was_replaced_is_given_back(vault, monkeypatch):
    """The holder before deletes the lock file as it lets go, and a process that had the
    file open then holds a lock on a file nobody else will ever open. Here the check
    that tells the two files apart says so once: the lock taken on the first file has to
    be given back (taking it again would be refused otherwise), and the second one kept."""
    lock = open_month()
    real, asked = os.path.samestat, []

    def samestat(a, b):
        asked.append(real(a, b))
        return len(asked) > 1 and asked[-1]

    monkeypatch.setattr(store.os.path, "samestat", samestat)
    mine = _MonthLock(lock, wait=False)
    assert mine.acquire() and asked == [True, True]
    assert not _MonthLock(lock, wait=False).acquire()           # and it is held
    mine.release()
    assert not lock.exists()


# --- what is not a busy month -------------------------------------------------------------

def test_a_lock_file_that_cannot_be_opened_is_an_error(vault, monkeypatch):
    lock = open_month()
    monkeypatch.setattr(store, "_OPEN_PATIENCE", 0.1)
    with monkeypatch.context() as m:
        m.setattr(store.os, "open", failing(os.open, lock, PermissionError(errno.EACCES, "denied")))
        t0 = time.time()
        with pytest.raises(PermissionError):
            compact_month("t", RES, MONTH)          # wait=True: it must not wait ten minutes
        assert time.time() - t0 < 30
    assert len(month_files("t", RES, MONTH)) == 2 and free(lock)


def test_a_file_system_without_locks_is_an_error(vault, monkeypatch):
    lock = open_month()
    module, name = ("msvcrt", "locking") if os.name == "nt" else ("fcntl", "flock")

    def no_locks(*args):
        raise OSError(errno.ENOLCK, "no locks available")

    with monkeypatch.context() as m:
        m.setattr(f"{module}.{name}", no_locks)
        with pytest.raises(OSError) as raised:
            compact_month("t", RES, MONTH, wait=False)
        assert raised.value.errno == errno.ENOLCK
    assert free(lock) and not lock.exists()         # the file it opened was closed again


def test_a_failure_after_the_lock_is_taken_gives_it_back(vault, monkeypatch):
    lock = open_month()

    def disk_full(fd, data):
        raise OSError(errno.ENOSPC, "no space left on device")

    with monkeypatch.context() as m:
        m.setattr(store.os, "write", disk_full)
        with pytest.raises(OSError):
            _MonthLock(lock, wait=False).acquire()
    assert free(lock)                               # not held for the rest of the process
    sealed = compact_month("t", RES, MONTH, wait=False)
    assert month_files("t", RES, MONTH) == [sealed]


def test_a_lock_file_that_cannot_be_deleted_stays_and_locks_nothing(vault, monkeypatch):
    lock = open_month()
    mine = _MonthLock(lock, wait=False)
    assert mine.acquire()
    with monkeypatch.context() as m:
        m.setattr(store.os, "unlink", failing(os.unlink, lock, OSError(errno.EROFS, "read-only")))
        mine.release()                              # does not raise
    assert lock.exists() and free(lock)
    assert not lock.exists()
