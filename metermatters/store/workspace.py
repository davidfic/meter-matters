"""A user's files, wherever they are actually kept.

Everything that reads a bill reads a file: the statement history is the
utility's own CSV, usage arrives as the utility's own zips. That code takes
paths and should go on taking paths.

With a disk, a user's workspace is simply their folder. On Postgres there is
no folder, so one is made for the length of a request: the user's files are
written out to a temporary directory, the code runs against it unchanged, and
whatever it changed is written back - in the same transaction it was read in,
under a lock, so two requests for one user can't both start from the old
files and have the second erase the first.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from metermatters.store import accounts
from metermatters.store import db
from metermatters.store import storage

# Lock sidecars and half-written files are scaffolding, not data.
def _is_data(relative: str) -> bool:
    return not any(part.startswith(".") for part in relative.split("/"))


def _snapshot(root: Path) -> dict[str, bytes]:
    found = {}
    for item in root.rglob("*"):
        relative = item.relative_to(root).as_posix()
        if item.is_file() and _is_data(relative):
            found[relative] = item.read_bytes()
    return found


class Workspace:
    """Open with `open_`, or drive by hand with enter / commit / close."""

    def __init__(self, user, write: bool):
        self.user, self.write = user, write
        self.paths = None
        self._stack = contextlib.ExitStack()
        self._conn = self._root = None
        self._before: dict[str, str] = {}

    def enter(self) -> "Workspace":
        if self.user is accounts.LOCAL or self.user is None:
            self.paths = accounts.local_paths()
        elif not db.is_postgres():
            self.paths = self.user.paths
            if self.write:
                # One writer per user at a time, as on Postgres: settings,
                # the home list and held bills are read-modify-write files.
                self._stack.enter_context(storage.locked(self.user.root / ".workspace"))
        else:
            try:
                self._check_out()
            except BaseException:
                self.close(failed=True)
                raise
        return self

    def _check_out(self) -> None:
        self._manager = db.connect()
        self._conn = self._manager.__enter__()
        if self.write:
            self._conn.lock(self.user.id)
        self._root = Path(tempfile.mkdtemp(prefix="elec-ws-"))
        rows = self._conn.execute("SELECT name, content FROM files WHERE user_id = ?",
                                  (self.user.id,)).fetchall()
        for row in rows:
            content = bytes(row["content"])
            target = self._root / row["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            self._before[row["name"]] = hashlib.sha256(content).hexdigest()
        self.paths = accounts.Paths.under(self._root)
        if not self.write:
            # A read has what it came for. Holding the connection for the rest
            # of the request would starve the pool - the page may open another.
            manager, self._manager, self._conn = self._manager, None, None
            manager.__exit__(None, None, None)

    def commit(self) -> None:
        """Write back what changed. A no-op for a read, or with a disk."""
        if self._conn is None or not self.write:
            return
        after = _snapshot(self._root)
        for name, content in after.items():
            if self._before.get(name) != hashlib.sha256(content).hexdigest():
                self._conn.execute(
                    "INSERT INTO files (user_id, name, content, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (user_id, name) DO UPDATE SET "
                    "content = excluded.content, updated_at = excluded.updated_at",
                    (self.user.id, name, content, db.now()))
        for name in self._before.keys() - after.keys():
            self._conn.execute("DELETE FROM files WHERE user_id = ? AND name = ?",
                               (self.user.id, name))
        self._before = {n: hashlib.sha256(c).hexdigest() for n, c in after.items()}

    def close(self, failed: bool = False) -> None:
        """End the transaction - releasing the lock - and remove the folder.

        A commit that fails raises: the caller must not report success.
        """
        manager, self._conn = getattr(self, "_manager", None), None
        self._manager = None
        try:
            if manager is not None:
                if failed:
                    try:
                        manager.__exit__(RuntimeError, RuntimeError("workspace abandoned"), None)
                    except Exception:           # noqa: BLE001 - already failing; the rollback is best effort
                        pass
                else:
                    manager.__exit__(None, None, None)
        finally:
            self._stack.close()
            if self._root is not None:
                shutil.rmtree(self._root, ignore_errors=True)
                self._root = None


@contextlib.contextmanager
def open_(user, write: bool = False):
    """The user's files as paths. Changes are kept only if `write` and no error."""
    space = Workspace(user, write).enter()
    try:
        yield space.paths
        space.commit()
    except BaseException:
        space.close(failed=True)
        raise
    space.close()


def inventory(user) -> list[dict]:
    """What a user is storing: each file's path, size and when it last changed.

    Names, sizes and dates only. Nothing here reads a file's contents, so it
    is safe to call on behalf of someone who is not that user.
    """
    if db.is_postgres():
        with db.connect() as conn:
            rows = conn.execute("SELECT name, octet_length(content) AS size, updated_at "
                                "FROM files WHERE user_id = ?", (user.id,)).fetchall()
        return [{"name": row["name"], "size": int(row["size"]), "updated": row["updated_at"]}
                for row in rows]
    root = user.root
    if not root.exists():
        return []
    found = []
    for item in root.rglob("*"):
        relative = item.relative_to(root).as_posix()
        if item.is_file() and _is_data(relative):
            stat = item.stat()
            changed = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            found.append({"name": relative, "size": stat.st_size,
                          "updated": changed.isoformat(timespec="seconds")})
    return found
