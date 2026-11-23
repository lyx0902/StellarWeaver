"""Bounded UTF-8 reads and version-checked, exact single-file changes."""

from __future__ import annotations

import ast
import asyncio
import codecs
import difflib
import fnmatch
import hashlib
import io
import os
import stat
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class EditSpan(Arguments):
    old_text: str = Field(min_length=1, max_length=MAX_EDIT_BYTES)
    new_text: str = Field(max_length=MAX_EDIT_BYTES)


class MultiEditFileArgs(Arguments):
    path: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_text: str | None = Field(default=None, min_length=1, max_length=MAX_EDIT_BYTES)
    new_text: str | None = Field(default=None, max_length=MAX_EDIT_BYTES)
    edits: list[EditSpan] | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_form(self) -> MultiEditFileArgs:
        if self.edits is not None:
            if self.model_fields_set.intersection({"old_text", "new_text"}):
                raise ValueError("Send edits or old_text and new_text")
        elif self.old_text is None or self.new_text is None:
            raise ValueError("Send edits or both old_text and new_text")
        return self


def syntax_diagnostic(
    ctx: ToolContext, path: Path, relative: str, content: bytes
) -> dict[str, Any]:
    if not ctx.post_edit_syntax_hook or path.suffix != ".py":
        return {}
    try:
        ast.parse(content, filename=relative)
    except SyntaxError as exc:
        return {
            "syntax_diagnostic": {
                "code": "PYTHON_SYNTAX_ERROR",
                "message": exc.msg,
                "path": relative,
                "line": exc.lineno,
                "column": exc.offset,
                "end_line": exc.end_lineno,
                "end_column": exc.end_offset,
            }
        }
    except RecursionError:
        return {
            "syntax_diagnostic": {
                "code": "PYTHON_SYNTAX_CHECK_INCOMPLETE",
                "message": "Source exceeds the Python parser recursion limit.",
                "path": relative,
            }
        }
    return {}


def syntax_hint(diagnostic: dict[str, Any]) -> str:
    line = diagnostic.get("line")
    location = f"line {line}: " if line is not None else ""
    return f"\n[syntax_diagnostic] {location}{diagnostic['message']}"


def _diff(path: str, old: str, new: str, *, mark_unterminated: bool = False) -> str:
    lines = difflib.unified_diff(
        io.StringIO(old, newline="\n").readlines() if mark_unterminated else old.splitlines(True),
        io.StringIO(new, newline="\n").readlines() if mark_unterminated else new.splitlines(True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    if mark_unterminated:
        return "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in lines
        )
    return "".join(lines)


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

    def listing() -> dict[str, Any]:
        ctx.workspace.recheck(args.path, root, allow_directory=True)
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

    async def execute() -> dict[str, Any]:
        return await asyncio.to_thread(listing) if ctx.parallel_read else listing()

    return PreparedTool({"path": ctx.workspace.relative(root)}, execute)


async def prepare_read_file(args: ReadFileArgs, ctx: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    path = ctx.workspace.resolve(args.path)
    end = args.end_line if args.end_line is not None else args.start_line + 399
    if end < args.start_line or end - args.start_line >= 400:
        raise ToolError(
            "INVALID_ARGUMENTS", "read_file requires an ordered range of at most 400 lines."
        )

    def read() -> dict[str, Any]:
        ctx.workspace.recheck(args.path, path)
        data = _scan_file(path, args.start_line, end)
        data["path"] = ctx.workspace.relative(path)
        text = "\n".join(f"{row['line']}: {row['text']}" for row in data["lines"])
        return {"content": text, "data": data}

    async def execute() -> dict[str, Any]:
        return await asyncio.to_thread(read) if ctx.parallel_read else read()

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
        syntax = syntax_diagnostic(ctx, path, relative, content)
        hint = syntax_hint(syntax["syntax_diagnostic"]) if syntax else ""
        return {
            "content": f"Created {relative}\n{diff}{hint}",
            "data": {
                "path": relative,
                "sha256": sha256_bytes(content),
                "before_sha256": None,
                "mode": stat.S_IMODE(path.stat().st_mode),
                "after_sha256": sha256_bytes(content),
                "diff": diff,
                "operation_id": ctx.operation_id,
                "diff_artifact_id": str(artifact),
                **syntax,
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


async def prepare_edit_file(
    args: EditFileArgs | MultiEditFileArgs, ctx: ToolContext
) -> PreparedTool:
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
    batch = isinstance(args, MultiEditFileArgs) and args.edits is not None
    if isinstance(args, MultiEditFileArgs) and args.edits is not None:
        edits = args.edits
    else:
        assert args.old_text is not None and args.new_text is not None
        edits = [EditSpan(old_text=args.old_text, new_text=args.new_text)]
    crlf = "\r\n" in text and "\n" not in text.replace("\r\n", "")
    spans: list[tuple[int, int, str, int]] = []
    for index, edit in enumerate(edits, 1):
        count = text.count(edit.old_text)
        details: dict[str, Any] = {"edit_index": index} if batch else {}
        if count == 0:
            raise ToolError(
                "EDIT_NOT_FOUND", "old_text does not occur exactly in this file.", details
            )
        if count > 1:
            raise ToolError(
                "EDIT_AMBIGUOUS",
                "old_text occurs more than once; provide a larger unique context.",
                {**details, "matches": count},
            )
        start = text.index(edit.old_text)
        replacement = edit.new_text
        if crlf:
            replacement = replacement.replace("\r\n", "\n").replace("\n", "\r\n")
        spans.append((start, start + len(edit.old_text), replacement, index))
    parts: list[str] = []
    previous_end = 0
    for start, end, replacement, index in sorted(spans):
        if start < previous_end:
            raise ToolError(
                "EDIT_OVERLAPPING",
                "Edit ranges overlap in the original file.",
                {"edit_index": index},
            )
        parts.extend((text[previous_end:start], replacement))
        previous_end = end
    parts.append(text[previous_end:])
    new_text = "".join(parts)
    new_bytes = new_text.encode("utf-8")
    if len(new_bytes) > MAX_EDIT_BYTES:
        raise ToolError("FILE_TOO_LARGE", "Result exceeds the 1 MiB edit limit.")
    relative = ctx.workspace.relative(path)
    diff = _diff(relative, text, new_text, mark_unterminated=batch)

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
        syntax = syntax_diagnostic(ctx, path, relative, new_bytes)
        hint = syntax_hint(syntax["syntax_diagnostic"]) if syntax else ""
        return {
            "content": f"Edited {relative}\n{diff}{hint}",
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
                "replacement_count": len(spans),
                "operation_id": ctx.operation_id,
                "diff_artifact_id": str(artifact),
                **syntax,
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


def register_tools(
    registry: ToolRegistry, *, multi_edit: bool = False, readonly_only: bool = False
) -> None:
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
    if readonly_only:
        return
    registry.register(
        "create_file",
        "Exclusively create a new UTF-8 file after authorization.",
        CreateFileArgs,
        prepare_create_file,
    )
    registry.register(
        "edit_file",
        (
            "Edit one file. Send old_text and new_text or edits with unique, non-overlapping "
            "old_text spans from the original file, and its expected_sha256."
            if multi_edit
            else "Replace one exact unique text span after checking expected_sha256."
        ),
        MultiEditFileArgs if multi_edit else EditFileArgs,
        prepare_edit_file,
    )
