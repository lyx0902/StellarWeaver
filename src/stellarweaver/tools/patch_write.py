"""File manifests, durable replacements and evidence-based patch recovery."""

import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from stellarweaver.domain import JSONDict, ToolStatus
from stellarweaver.tools.files import fsync_directory
from stellarweaver.tools.workspace import ToolError, Workspace, sha256_bytes

PATCH_LIMIT = 60 * 1024
NEW_MODE = 0o666 if os.name == "nt" else 0o644


@dataclass(frozen=True)
class FileState:
    content: bytes | None = None
    mode: int | None = None

    @property
    def digest(self) -> str | None:
        return sha256_bytes(self.content) if self.content is not None else None


def direct_target(workspace: Workspace, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not path.parts
        or path.as_posix() != relative
        or any(ord(ch) < 32 or ch in '\\:"<>|?*' for ch in relative)
        or any(
            part.rstrip(" .") != part
            or part.casefold() in {"..", ".gitattributes", ".gitmodules", "agents.md"}
            or PureWindowsPath(part).is_reserved()
            for part in path.parts
        )
    ):
        raise ToolError("INVALID_PATH", "Use a normalized project file path.")
    target = workspace.resolve(relative, must_exist=False)
    raw = workspace.root / relative
    if target != raw or any(
        item.is_symlink()
        for item in (raw, *raw.parents)
        if item != workspace.root and item.is_relative_to(workspace.root)
    ):
        raise ToolError("INVALID_PATH", "Patch operations require direct regular-file paths.")
    return target


def read_target(workspace: Workspace, relative: str) -> FileState:
    target = direct_target(workspace, relative)
    if not target.exists():
        return FileState()
    info = target.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ToolError("PATCH_UNSUPPORTED", "Patch operations require unlinked regular files.")
    with target.open("rb") as source:
        data = source.read(PATCH_LIMIT + 1)
    if len(data) > PATCH_LIMIT or b"\0" in data:
        raise ToolError("PATCH_UNSUPPORTED", "Patch operations require bounded UTF-8 text files.")
    try:
        data.decode()
    except UnicodeDecodeError as exc:
        raise ToolError("PATCH_UNSUPPORTED", "Patch operations require UTF-8 text.") from exc
    return FileState(data, stat.S_IMODE(info.st_mode))


@dataclass(frozen=True)
class FileWrite:
    path: str
    before: FileState
    after: FileState


def _temporary(path: Path, state: FileState) -> Path:
    assert state.content is not None and state.mode is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".sw-patch-", delete=False) as fp:
            temporary = Path(fp.name)
            fp.write(state.content)
            fp.flush()
            os.chmod(temporary, state.mode)
            os.fsync(fp.fileno())
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return temporary


def write_files(workspace: Workspace, changes: list[FileWrite], *, conflict: str) -> None:
    """Stage all replacement bytes before swapping targets and compensate ordinary I/O errors."""
    completed: list[FileWrite] = []
    staged: dict[str, Path] = {}

    def check(change: FileWrite) -> None:
        if read_target(workspace, change.path) != change.before:
            raise ToolError(
                conflict, "A patch target changed before writing.", {"path": change.path}
            )

    try:
        for change in changes:
            check(change)
        for change in changes:
            if change.after.content is not None:
                staged[change.path] = _temporary(
                    direct_target(workspace, change.path), change.after
                )
        for change in changes:
            check(change)
        for change in changes:
            check(change)
            target = direct_target(workspace, change.path)
            if change.after.content is None:
                target.unlink()
            else:
                os.replace(staged[change.path], target)
            completed.append(change)
            fsync_directory(target.parent)
    except (OSError, ToolError) as exc:
        if not completed:
            raise
        try:
            for change in reversed(completed):
                if read_target(workspace, change.path) != change.after:
                    raise OSError(f"Patch target changed during recovery: {change.path}")
                target = direct_target(workspace, change.path)
                if change.before.content is None:
                    target.unlink()
                else:
                    temp = _temporary(target, change.before)
                    try:
                        os.replace(temp, target)
                    finally:
                        temp.unlink(missing_ok=True)
                fsync_directory(target.parent)
        except (OSError, ToolError) as rollback:
            raise OSError(
                "Patch write was interrupted and compensation was incomplete."
            ) from rollback
        raise ToolError(
            conflict if isinstance(exc, ToolError) else "PATCH_WRITE_ROLLED_BACK",
            "Patch writes were compensated after an error.",
        ) from exc
    finally:
        for path in staged.values():
            path.unlink(missing_ok=True)


def manifest(data: JSONDict) -> list[JSONDict]:
    entries = data.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
        raise ToolError("PATCH_METADATA_MISSING", "Patch operation requires a file manifest.")
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ToolError("PATCH_METADATA_MISSING", "Invalid patch manifest entry.")
        path, before, after, mode = (
            item.get(k) for k in ("path", "before_sha256", "after_sha256", "mode")
        )
        backup = item.get("before_artifact_id")
        action = "create" if before is None else "delete" if after is None else "modify"
        if (
            not isinstance(path, str)
            or not path
            or os.path.normcase(path) in seen
            or not {"before_sha256", "after_sha256", "before_artifact_id"} <= item.keys()
            or before == after
            or item.get("action") != action
            or type(mode) is not int
            or not 0 <= mode <= 0o7777
            or (before is None and backup is not None)
            or (before is not None and (not isinstance(backup, str) or not backup))
            or any(
                value is not None
                and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None)
                for value in (before, after)
            )
        ):
            raise ToolError("PATCH_METADATA_MISSING", "Inconsistent patch file manifest.")
        seen.add(os.path.normcase(path))
    return cast(list[JSONDict], entries)


def inspect_patch(
    workspace: Workspace, data: JSONDict, *, undo: bool = False
) -> tuple[ToolStatus, str, JSONDict]:
    """Classify the entire operation from disk without writing or replaying it."""
    observed: list[JSONDict] = []
    details: JSONDict = {
        "files_observed": observed,
        "recovered_from_state": False,
        "executed": None,
    }
    try:
        entries = manifest(data)
    except ToolError as exc:
        return "unknown", exc.message, {**details, "code": exc.code}
    for item in entries:
        state: Literal["before", "after", "other", "unavailable"] = "unavailable"
        actual = None
        try:
            current = read_target(workspace, item["path"])
            actual = current.digest
            state = "other"
            if current.mode is None or current.mode == item["mode"]:
                if actual == item["before_sha256"]:
                    state = "before"
                elif actual == item["after_sha256"]:
                    state = "after"
        except (OSError, ToolError, ValueError):
            pass
        observed.append({"path": item["path"], "state": state, "actual_sha256": actual})
    states = {item["state"] for item in observed}
    if states == {"before" if undo else "after"}:
        code = "UNDO_RECOVERED_FROM_STATE" if undo else "RECOVERED_FROM_STATE"
        return (
            "ok",
            "All patch targets match the intended final state.",
            {
                **data,
                **details,
                "code": code,
                "recovered_from_state": True,
                "executed": True,
            },
        )
    if states == {"after" if undo else "before"}:
        code = "UNDO_RECOVERY_NOT_EXECUTED" if undo else "RECOVERED_NOT_EXECUTED"
        return (
            ("blocked" if undo else "cancelled"),
            "All patch targets match the initial state.",
            {
                **details,
                "code": code,
                "executed": False,
            },
        )
    return (
        "unknown",
        "Patch targets are mixed or changed. Inspect the workspace.",
        {
            **data,
            **details,
            "code": "UNDO_RECOVERY_CONFLICT" if undo else "RECOVERY_PATCH_MIXED",
        },
    )
