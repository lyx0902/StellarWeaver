"""Bounded UTF-8 reads and version-checked, exact single-file changes."""

from __future__ import annotations

import codecs
import difflib
import fnmatch
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from stellarweaver.tools.workspace import MAX_EDIT_BYTES, ToolError, sha256_bytes

if TYPE_CHECKING:
    from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListFilesArgs(Arguments):
    path: str = "."
    glob: str | None = None
    limit: int = Field(default=200, ge=1, le=2000)


class ReadFileArgs(Arguments):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class CreateFileArgs(Arguments):
    path: str
    content: str = Field(max_length=MAX_EDIT_BYTES)


class EditFileArgs(Arguments):
    path: str
    old_text: str = Field(min_length=1, max_length=MAX_EDIT_BYTES)
    new_text: str = Field(max_length=MAX_EDIT_BYTES)
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def fsync_directory(path: Path) -> None:
    """Persist directory entry changes where the platform supports directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_durable(path: Path, data: bytes, *, exclusive: bool = True) -> None:
    with path.open("xb" if exclusive else "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(path.parent)


def _text_bytes(path: Path) -> tuple[bytes, str]:
    if path.stat().st_size > MAX_EDIT_BYTES:
        raise ToolError("FILE_TOO_LARGE", "Built-in edits support UTF-8 files up to 1 MiB.")
    with path.open("rb") as stream:
        content = stream.read(MAX_EDIT_BYTES + 1)
    if len(content) > MAX_EDIT_BYTES:
        raise ToolError("FILE_TOO_LARGE", "File grew beyond the 1 MiB edit limit.")
    if b"\0" in content:
        raise ToolError("BINARY_FILE", "Binary files cannot be edited as UTF-8 text.")
    try:
        return content, content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("INVALID_ENCODING", "File is not valid UTF-8.") from exc


def _scan_file(path: Path, start: int, end: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    selected: list[dict[str, Any]] = []
    line_no = 1
    line = ""
    line_chars = 0
    clipped = False
    output_chars = 0

    def add_line() -> None:
        nonlocal output_chars, clipped
        if start <= line_no <= end:
            text = line.rstrip("\r")
            remaining = max(0, 65536 - output_chars)
            if len(text) > remaining or line_chars > len(line):
                clipped = True
            text = text[:remaining]
            selected.append({"line": line_no, "text": text})
            output_chars += len(text)

    try:
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                digest.update(chunk)
                if b"\0" in chunk:
                    raise ToolError("BINARY_FILE", "Binary files cannot be read as UTF-8 text.")
                pieces = decoder.decode(chunk).split("\n")
                for index, piece in enumerate(pieces):
                    line_chars += len(piece)
                    if len(line) < 16384:
                        line += piece[: 16384 - len(line)]
                    if index < len(pieces) - 1:
                        add_line()
                        line_no += 1
                        line = ""
                        line_chars = 0
            tail = decoder.decode(b"", final=True)
            line_chars += len(tail)
            line += tail[: max(0, 16384 - len(line))]
            if line_chars:
                add_line()
            total_lines = line_no if line_chars else line_no - 1
    except UnicodeDecodeError as exc:
        raise ToolError("INVALID_ENCODING", "File is not valid UTF-8.") from exc
    return {
        "sha256": digest.hexdigest(),
        "lines": selected,
        "start_line": start,
        "end_line": min(end, total_lines),
        "total_lines": total_lines,
        "truncated": clipped or total_lines > end,
        "editable": path.stat().st_size <= MAX_EDIT_BYTES,
    }


async def prepare_list_files(args: ListFilesArgs, ctx: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    root = ctx.workspace.resolve(args.path, allow_directory=True)

    async def execute() -> dict[str, Any]:
        paths: list[str] = []
        truncated = False
        if root.is_file():
            candidates = [root]
            for candidate in candidates:
                relative = ctx.workspace.relative(candidate)
                if not ctx.workspace.is_ignored(relative) and (
                    args.glob is None or fnmatch.fnmatch(relative, args.glob)
                ):
                    paths.append(relative)
        else:
            for directory, folders, files in os.walk(root, followlinks=False):
                parent = Path(directory)
                folders[:] = sorted(
                    folder
                    for folder in folders
                    if not ctx.workspace.is_ignored(
                        ctx.workspace.relative(parent / folder), is_dir=True
                    )
                    and not (parent / folder).is_symlink()
                )
                for filename in sorted(files):
                    candidate = parent / filename
                    relative = ctx.workspace.relative(candidate)
                    if (
                        candidate.is_symlink()
                        or ctx.workspace.is_ignored(relative)
                        or (args.glob is not None and not fnmatch.fnmatch(relative, args.glob))
                    ):
                        continue
                    if len(paths) >= args.limit:
                        truncated = True
                        break
                    paths.append(relative)
                if truncated:
                    break
        return {
            "content": "\n".join(paths),
            "data": {"paths": paths, "count": len(paths), "truncated": truncated},
        }

    return PreparedTool({"path": args.path}, execute)


async def prepare_read_file(args: ReadFileArgs, ctx: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    path = ctx.workspace.resolve(args.path)
    end = args.end_line if args.end_line is not None else args.start_line + 399
    if end < args.start_line or end - args.start_line >= 400:
        raise ToolError(
            "INVALID_ARGUMENTS", "read_file requires an ordered range of at most 400 lines."
        )

    async def execute() -> dict[str, Any]:
        checked = ctx.workspace.resolve(args.path)
        data = _scan_file(checked, args.start_line, end)
        data["path"] = ctx.workspace.relative(path)
        text = "\n".join(f"{row['line']}: {row['text']}" for row in data["lines"])
        return {"content": text, "data": data}

    return PreparedTool({"path": ctx.workspace.relative(path)}, execute)


async def prepare_create_file(args: CreateFileArgs, ctx: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    path = ctx.workspace.resolve(args.path, must_exist=False)
    if path.exists():
        raise ToolError("FILE_EXISTS", "create_file never overwrites an existing path.")
    content = args.content.encode("utf-8")
    if len(content) > MAX_EDIT_BYTES:
        raise ToolError("FILE_TOO_LARGE", "New UTF-8 files may contain at most 1 MiB.")
    relative = ctx.workspace.relative(path)
    diff = _diff(relative, "", args.content)

    async def recheck() -> None:
        checked = ctx.workspace.resolve(args.path, must_exist=False)
        if checked != path or checked.exists():
            raise ToolError("FILE_CHANGED", "New-file target changed during authorization.")

    async def execute() -> dict[str, Any]:
        await recheck()
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx.workspace.resolve(args.path, must_exist=False)
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact = ctx.artifacts_dir / f"{ctx.operation_id}.diff"
        write_durable(artifact, diff.encode("utf-8"))
        try:
            with path.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError as exc:
            raise ToolError("FILE_EXISTS", "New-file target already exists.") from exc
        fsync_directory(path.parent)
        return {
            "content": f"Created {relative}\n{diff}",
            "data": {
                "path": relative,
                "sha256": sha256_bytes(content),
                "before_sha256": None,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "after_sha256": sha256_bytes(content),
                "diff": diff,
                "operation_id": ctx.operation_id,
                "diff_artifact_id": str(artifact),
            },
            "artifact_ids": (str(artifact),),
        }

    return PreparedTool(
        {
            "path": relative,
            "preview": diff,
            "before_sha256": None,
            "after_sha256": sha256_bytes(content),
            "diff_artifact_id": str(ctx.artifacts_dir / f"{ctx.operation_id}.diff"),
        },
        execute,
        recheck,
    )


async def prepare_edit_file(args: EditFileArgs, ctx: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    path = ctx.workspace.resolve(args.path)
    old_bytes, text = _text_bytes(path)
    old_hash = sha256_bytes(old_bytes)
    original_mode = stat.S_IMODE(path.stat().st_mode)
    if old_hash != args.expected_sha256:
        raise ToolError(
            "FILE_CHANGED",
            "File content changed after the earlier read.",
            {"path": args.path, "expected_sha256": args.expected_sha256, "actual_sha256": old_hash},
        )
    count = text.count(args.old_text)
    if count == 0:
        raise ToolError("EDIT_NOT_FOUND", "old_text does not occur exactly in this file.")
    if count > 1:
        raise ToolError(
            "EDIT_AMBIGUOUS",
            "old_text occurs more than once; provide a larger unique context.",
            {"matches": count},
        )
    replacement = args.new_text
    if "\r\n" in text and "\n" not in text.replace("\r\n", ""):
        replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
    new_text = text.replace(args.old_text, replacement, 1)
    new_bytes = new_text.encode("utf-8")
    if len(new_bytes) > MAX_EDIT_BYTES:
        raise ToolError("FILE_TOO_LARGE", "Result exceeds the 1 MiB edit limit.")
    relative = ctx.workspace.relative(path)
    diff = _diff(relative, text, new_text)

    async def recheck() -> None:
        checked = ctx.workspace.resolve(args.path)
        current, _ = _text_bytes(checked)
        actual = sha256_bytes(current)
        if (
            checked != path
            or actual != old_hash
            or stat.S_IMODE(checked.stat().st_mode) != original_mode
        ):
            raise ToolError(
                "FILE_CHANGED",
                "File changed during authorization; read it again before editing.",
                {"expected_sha256": old_hash, "actual_sha256": actual},
            )

    async def execute() -> dict[str, Any]:
        await recheck()
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        backup = ctx.artifacts_dir / f"{ctx.operation_id}.before"
        write_durable(backup, old_bytes)
        artifact = ctx.artifacts_dir / f"{ctx.operation_id}.diff"
        write_durable(artifact, diff.encode("utf-8"))
        mode = original_mode
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".stellarweaver-edit-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(new_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            await recheck()
            os.replace(temporary, path)
            fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        new_hash = sha256_bytes(new_bytes)
        return {
            "content": f"Edited {relative}\n{diff}",
            "data": {
                "path": relative,
                "old_hash": old_hash,
                "new_hash": new_hash,
                "before_sha256": old_hash,
                "before_artifact_id": str(backup),
                "mode": original_mode,
                "after_sha256": new_hash,
                "sha256": new_hash,
                "diff": diff,
                "replacement_count": 1,
                "operation_id": ctx.operation_id,
                "diff_artifact_id": str(artifact),
            },
            "artifact_ids": (str(backup), str(artifact)),
        }

    return PreparedTool(
        {
            "path": relative,
            "preview": diff,
            "expected_sha256": old_hash,
            "before_sha256": old_hash,
            "after_sha256": sha256_bytes(new_bytes),
            "before_artifact_id": str(ctx.artifacts_dir / f"{ctx.operation_id}.before"),
            "diff_artifact_id": str(ctx.artifacts_dir / f"{ctx.operation_id}.diff"),
            "mode": original_mode,
        },
        execute,
        recheck,
    )


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        "list_files",
        "List workspace files respecting ignore and protected-path rules.",
        ListFilesArgs,
        prepare_list_files,
    )
    registry.register(
        "read_file",
        "Read at most 400 numbered UTF-8 lines and the full file SHA-256.",
        ReadFileArgs,
        prepare_read_file,
    )
    registry.register(
        "create_file",
        "Exclusively create a new UTF-8 file after authorization.",
        CreateFileArgs,
        prepare_create_file,
    )
    registry.register(
        "edit_file",
        "Replace one exact unique text span after checking expected_sha256.",
        EditFileArgs,
        prepare_edit_file,
    )
