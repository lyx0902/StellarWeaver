"""Bounded, content-addressed snapshots for the private verification transport."""

import hashlib
import io
import json
import os
import stat
import tarfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import BinaryIO
from uuid import uuid4

from stellarweaver.tools.workspace import ToolError, Workspace

MAX_FILES = 6000
MAX_BYTES = 32 * 1024 * 1024
ARCHIVE_LIMIT = MAX_BYTES + 12 * 1024 * 1024
_DELETED = ".stellarweaver/snapshot-deleted.json"
Manifest = dict[str, tuple[str, int, int]]


class SnapshotMismatch(ValueError):
    """The content-addressed transfer must be retried from a full snapshot."""


class SnapshotCache:
    """A bounded LRU of immutable, checked tars. No persistent disk storage."""

    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024, max_entries: int = 8) -> None:
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.entries: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self.size = 0
        self.lock = Lock()

    def get(self, scope: str, digest: str) -> bytes | None:
        key = scope, digest
        with self.lock:
            if key in self.entries:
                self.entries.move_to_end(key)
            return self.entries.get(key)

    def put(self, scope: str, digest: str, data: bytes) -> None:
        if len(data) > self.max_bytes:
            return
        key = scope, digest
        with self.lock:
            self.size += len(data) - len(self.entries.pop(key, b""))
            self.entries[key] = data
            while self.size > self.max_bytes or len(self.entries) > self.max_entries:
                _, old = self.entries.popitem(last=False)
                self.size -= len(old)


def manifest_digest(manifest: Manifest) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _metadata(data: bytes, mode: int) -> tuple[str, int, int]:
    return hashlib.sha256(data).hexdigest(), mode, len(data)


def _safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        name == path.as_posix()
        and bool(path.parts)
        and not path.is_absolute()
        and ".." not in path.parts
        and not any(item in name for item in "\\:\0")
        and not Workspace.is_protected(name)
    )


def _add(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


@dataclass(frozen=True)
class Transfer:
    manifest: Manifest
    digest: str
    base: str | None
    size: int
    changed: int

    def metrics(self) -> dict[str, str | int]:
        return {
            "snapshot_mode": "delta" if self.base else "full",
            "transfer_bytes": self.size,
            "changed_files_count": self.changed,
        }


@dataclass
class SnapshotState:
    scope: str = field(default_factory=lambda: uuid4().hex)
    previous: Transfer | None = None


def pack_transfer(
    root: Path, files: list[tuple[str, Path]], target: BinaryIO, previous: Transfer | None
) -> Transfer:
    workspace = Workspace(root)
    manifest: Manifest = {}
    changed = 0
    total = 0
    old = previous.manifest if previous else {}
    target.seek(0)
    target.truncate()
    with tarfile.open(fileobj=target, mode="w") as tar:
        for relative, path in files:
            original = path.lstat()
            with path.open("rb") as stream:
                current = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(original.st_mode)
                    or not os.path.samestat(original, current)
                    or workspace.resolve(relative) != path
                    or current.st_nlink != 1
                    or not _safe_path(relative)
                ):
                    raise ToolError("SNAPSHOT_UNSAFE_ENTRY", "The snapshot file identity changed.")
                data = stream.read(MAX_BYTES - total + 1)
                after = os.fstat(stream.fileno())
                if current.st_mtime_ns != after.st_mtime_ns or after.st_size != len(data):
                    raise ToolError("SNAPSHOT_CHANGED", "A snapshot file changed while being read.")
            total += len(data)
            if total > MAX_BYTES or len(manifest) >= MAX_FILES:
                raise ToolError("WORKTREE_LIMIT", "The test snapshot exceeds the workspace limit.")
            mode = 0o755 if current.st_mode & stat.S_IXUSR else 0o644
            meta = manifest[relative] = _metadata(data, mode)
            if old.get(relative) != meta:
                _add(tar, relative, data, mode)
                changed += 1
        if previous:
            removed = sorted(old.keys() - manifest.keys())
            _add(tar, _DELETED, json.dumps(removed, separators=(",", ":")).encode(), 0o644)
            changed += len(removed)
    size = target.tell()
    target.seek(0)
    return Transfer(
        manifest, manifest_digest(manifest), previous.digest if previous else None, size, changed
    )


def _read(
    source: BinaryIO, *, delta: bool = False
) -> tuple[dict[str, tuple[bytes, int]], set[str]]:
    source.seek(0)
    contents: dict[str, tuple[bytes, int]] = {}
    removed: set[str] | None = None
    size = 0
    with tarfile.open(fileobj=source, mode="r:") as tar:
        for entry in tar:
            name = entry.name
            if not entry.isfile() or entry.sparse or entry.mode not in (0o644, 0o755):
                raise ValueError("Invalid snapshot entry type or mode")
            if delta and name == _DELETED and removed is None:
                if not 0 <= entry.size <= ARCHIVE_LIMIT - MAX_BYTES:
                    raise ValueError("Invalid deletion manifest size")
                stream = tar.extractfile(entry)
                assert stream is not None
                paths = json.load(stream)
                if (
                    not isinstance(paths, list)
                    or len(paths) > MAX_FILES
                    or any(not isinstance(path, str) or not _safe_path(path) for path in paths)
                ):
                    raise ValueError("Invalid deletion manifest")
                removed = set(paths)
                if len(removed) != len(paths):
                    raise ValueError("Duplicate deletion")
                continue
            if (
                not _safe_path(name)
                or name in contents
                or entry.size < 0
                or size + entry.size > MAX_BYTES
                or len(contents) >= MAX_FILES
            ):
                raise ValueError("Invalid snapshot entry")
            stream = tar.extractfile(entry)
            assert stream is not None
            data = stream.read()
            contents[name] = (data, entry.mode)
            size += len(data)
    source.seek(0)
    if delta and removed is None:
        raise ValueError("Missing deletion manifest")
    return contents, removed or set()


def verify_transfer(
    upload: BinaryIO,
    target: BinaryIO,
    expected: str,
    *,
    baseline: bytes | None = None,
    base_digest: str | None = None,
) -> None:
    """Apply an untrusted delta in memory and validate all bytes and normalized permissions."""
    if base_digest is not None and baseline is None:
        raise SnapshotMismatch("Snapshot baseline unavailable")
    contents, removed = _read(upload, delta=base_digest is not None)
    if baseline is not None:
        original, _ = _read(io.BytesIO(baseline))
        if not removed <= original.keys() or removed.intersection(contents):
            raise SnapshotMismatch("Delta does not match its baseline")
        for path in removed:
            del original[path]
        original.update(contents)
        contents = original
    manifest = {path: _metadata(data, mode) for path, (data, mode) in contents.items()}
    if len(manifest) > MAX_FILES or sum(meta[2] for meta in manifest.values()) > MAX_BYTES:
        raise ValueError("Merged snapshot exceeds workspace limit")
    for name in contents:
        if any(parent.as_posix() in contents for parent in PurePosixPath(name).parents):
            raise ValueError("Snapshot has a file/directory conflict")
    if manifest_digest(manifest) != expected:
        raise SnapshotMismatch("Snapshot manifest does not match the host")
    with tarfile.open(fileobj=target, mode="w") as tar:
        for path, (data, mode) in sorted(contents.items()):
            _add(tar, path, data, mode)
    target.seek(0)
