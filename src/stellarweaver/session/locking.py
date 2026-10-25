"""OS-owned, nonblocking single-writer locks; lock files are never deleted."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


class SessionError(ValueError):
    """A session cannot be used safely."""


class SessionBusy(SessionError):
    """Another process owns the session writer lock."""


class SessionNotFound(SessionError):
    """No supported session database exists at the requested location."""


class SessionWorkspaceMismatch(SessionError):
    """The current workspace differs from the persisted session workspace."""


class WriterLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: BinaryIO | None = None

    def acquire(self) -> None:
        stream = self.path.open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                if stream.seek(0, 2) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise SessionBusy(f"Session is already in use: {self.path.parent.name}") from exc
        self.stream = stream

    def release(self) -> None:
        if self.stream is None:
            return
        stream, self.stream = self.stream, None
        try:
            if sys.platform == "win32":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


@contextmanager
def workspace_write_lock(workspace: Path, *, lock_root: Path | None = None) -> Iterator[None]:
    """Serialize agent writers across sessions using an OS-owned workspace key."""
    from platformdirs import user_data_path

    root = workspace.expanduser().resolve(strict=True)
    canonical = os.path.normcase(str(root))
    identity = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    directory = lock_root if lock_root is not None else user_data_path("stellarweaver") / "locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = WriterLock(directory / f"workspace-{identity}.lock")
    try:
        lock.acquire()
    except SessionBusy as exc:
        raise SessionBusy(f"Workspace is already in use by an agent writer: {root}") from exc
    try:
        yield
    finally:
        lock.release()
