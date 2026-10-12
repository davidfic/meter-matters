"""Writing files so a reader never sees half of one.

Two things write the same history: the monthly timer and the web app. Writing
in place means a crash - or the other writer - mid-write leaves a truncated
CSV, and a truncated CSV parses fine. It just has fewer bills in it.

So every write goes to a temporary file beside the target and is renamed over
it, which is atomic on POSIX: a reader sees the old file or the new one, never
a mixture. A read-modify-write cycle additionally holds :func:`locked`, so two
merges can't both read the old rows and have the second erase the first.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Same directory as the target: a rename is only atomic within a filesystem.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # mkstemp creates 0600; keep the target's own mode when it has one.
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


@contextlib.contextmanager
def locked(path):
    """Hold an exclusive lock for a read-modify-write of `path`.

    The lock is on a sidecar file, not the target - the target is replaced by
    rename, and a lock on the old inode would guard nothing. Not re-entrant:
    taking it twice for the same path in one call chain deadlocks.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / f".{path.name}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def try_locked(path):
    """Like :func:`locked`, but yields False at once if someone else holds it.

    For work that must happen once, not once per process: several web workers
    all wake to run the same job, and only the first should.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / f".{path.name}.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
