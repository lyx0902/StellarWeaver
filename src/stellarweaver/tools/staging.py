"""Prepare a bounded Git patch in disposable storage and export it after approval."""

import asyncio
import os
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from pydantic import Field

from stellarweaver.domain import JSONDict
from stellarweaver.tools.files import Arguments, syntax_diagnostic, syntax_hint, write_durable
from stellarweaver.tools.patch_write import (
    NEW_MODE,
    PATCH_LIMIT,
    FileState,
    FileWrite,
    direct_target,
    read_target,
    write_files,
)
from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry
from stellarweaver.tools.workspace import ToolError, sha256_bytes
from stellarweaver.web.isolation import git

_LIMIT = PATCH_LIMIT


class FileChange(Arguments):
    path: str = Field(min_length=1, max_length=240)
    expected_sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")
    content: str | None = Field(max_length=_LIMIT)


class StagePatchArgs(Arguments):
    changes: list[FileChange] = Field(min_length=1, max_length=12)


def _read(ctx: ToolContext, name: str) -> tuple[bytes | None, bool]:
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.as_posix() != name
        or any(char in name for char in "\\:\r\n\0\t")
        or any(
            part == ".." or part in {".gitattributes", ".gitmodules", "AGENTS.md"}
            for part in path.parts
        )
    ):
        raise ToolError(
            "INVALID_PATH", "Use a normalized project path outside repository metadata."
        )
    target = ctx.workspace.resolve(name, must_exist=False)
    if target != ctx.workspace.root / name:
        raise ToolError("INVALID_PATH", "Patch staging requires direct regular-file paths.")
    if not target.exists():
        return None, False
    if not target.is_file() or target.stat().st_nlink != 1:
        raise ToolError("PATCH_UNSUPPORTED", "Patch staging requires regular files.")
    with target.open("rb") as source:
        data = source.read(_LIMIT + 1)
    if len(data) > _LIMIT or b"\0" in data:
        raise ToolError("PATCH_UNSUPPORTED", "Patch staging requires bounded UTF-8 text files.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("PATCH_UNSUPPORTED", "Patch staging requires UTF-8 text.") from exc
    return data, bool(target.stat().st_mode & 0o100)


def _digest(content: bytes | None) -> str | None:
    return sha256_bytes(content) if content is not None else None


def _materialize(root: Path, name: str, data: bytes, executable: bool) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o755 if executable else 0o644)


def _prepare_patch(
    changes: list[FileChange], ctx: ToolContext
) -> tuple[str, bytes, dict[str, tuple[bytes | None, bool]], JSONDict]:
    base = git(ctx.workspace.root, "rev-parse", "HEAD").decode().strip()
    originals = {}
    replacements = {}
    for change in changes:
        if change.path in originals:
            raise ToolError("PATCH_DUPLICATE_PATH", "Each file must appear once in a staged patch.")
        original, executable = _read(ctx, change.path)
        if change.expected_sha256 != _digest(original):
            raise ToolError("FILE_CHANGED", "A patch target changed. Read it again before staging.")
        replacement = change.content.encode() if change.content is not None else None
        if replacement is not None and b"\0" in replacement:
            raise ToolError("PATCH_UNSUPPORTED", "Binary content is unsupported.")
        if replacement == original:
            raise ToolError("PATCH_EMPTY", "Each patch target must have an actual change.")
        originals[change.path] = (original, executable)
        replacements[change.path] = replacement
    with TemporaryDirectory(prefix="sw-stage-") as directory:
        build = Path(directory) / "build"
        check = Path(directory) / "check"
        build.mkdir()
        check.mkdir()
        for root in (build, check):
            git(root, "init", "--quiet", "--template=")
            for name, (content, executable) in originals.items():
                if content is not None:
                    _materialize(root, name, content, executable)
        base_paths = [name for name, (content, _) in originals.items() if content is not None]
        if base_paths:
            git(build, "add", "--force", "--", *base_paths)
        for name, content in replacements.items():
            if content is None:
                (build / name).unlink()
            else:
                _materialize(build, name, content, originals[name][1])
        added = [name for name in replacements if originals[name][0] is None]
        if added:
            git(build, "add", "--intent-to-add", "--force", "--", *added)
        patch = git(
            build,
            "diff",
            "--binary",
            "--full-index",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--",
            *sorted(originals),
        )
        if not patch or len(patch) > _LIMIT:
            raise ToolError("PATCH_TOO_LARGE", "A reviewable patch must be between 1 and 60 KiB.")
        try:
            git(check, "apply", "--check", "--whitespace=nowarn", "-", patch=patch)
            git(check, "apply", "--whitespace=nowarn", "-", patch=patch)
        except ToolError as exc:
            raise ToolError("PATCH_CONFLICT", "Git rejected the staged candidate.") from exc
        for name, content in replacements.items():
            path = check / name
            if (path.read_bytes() if path.is_file() else None) != content:
                raise ToolError(
                    "PATCH_CONFLICT", "The staged bytes differ from the requested change."
                )
    return (
        base,
        patch,
        originals,
        {
            name: {"before_sha256": _digest(originals[name][0]), "after_sha256": _digest(content)}
            for name, content in replacements.items()
        },
    )


async def prepare_stage_patch(args: StagePatchArgs, ctx: ToolContext) -> PreparedTool:
    base, raw, originals, changes = await asyncio.to_thread(_prepare_patch, args.changes, ctx)
    patch = raw.decode()
    metadata = {
        "base_commit": base,
        "patch_sha256": sha256_bytes(raw),
        "paths": sorted(changes),
        "changes": changes,
    }

    def check() -> None:
        if git(ctx.workspace.root, "rev-parse", "HEAD").decode().strip() != base or any(
            _read(ctx, name) != previous for name, previous in originals.items()
        ):
            raise ToolError(
                "PATCH_BASE_CHANGED", "The base or a patch target changed after preparation."
            )

    async def recheck() -> None:
        await asyncio.to_thread(check)

    def save() -> str:
        check()
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact = ctx.artifacts_dir / f"{ctx.operation_id}.staged.diff"
        write_durable(artifact, raw)
        return str(artifact)

    async def execute() -> JSONDict:
        artifact = await asyncio.to_thread(save)
        return {
            "data": {
                **metadata,
                "patch": patch,
                "stage_status": "staged",
                "workspace_applied": False,
            },
            "content": f"Staged {len(changes)} file(s) for review. The workspace was not changed.",
            "artifact_ids": (artifact,),
        }

    return PreparedTool({**metadata, "preview": patch}, execute, recheck)


async def prepare_apply_patch(args: StagePatchArgs, ctx: ToolContext) -> PreparedTool:
    names = [os.path.normcase(change.path) for change in args.changes]
    if len(set(names)) != len(names):
        raise ToolError("PATCH_DUPLICATE_PATH", "Each patch target must appear once.")
    for change in args.changes:
        direct_target(ctx.workspace, change.path)
    base, raw, _, hashes = await asyncio.to_thread(_prepare_patch, args.changes, ctx)
    files: list[JSONDict] = []
    writes: list[FileWrite] = []
    for number, change in enumerate(args.changes, 1):
        previous = read_target(ctx.workspace, change.path)
        if previous.digest != hashes[change.path]["before_sha256"]:
            raise ToolError("FILE_CHANGED", "A patch target changed during preparation.")
        mode = previous.mode if previous.mode is not None else NEW_MODE
        content = change.content.encode() if change.content is not None else None
        if content is not None and len(content) > PATCH_LIMIT:
            raise ToolError("PATCH_TOO_LARGE", "Replacement bytes exceed the per-file limit.")
        after = FileState(content, mode) if content is not None else FileState()
        backup = (
            str(ctx.artifacts_dir / f"{ctx.operation_id}.{number}.before")
            if previous.content is not None
            else None
        )
        files.append(
            {
                "path": change.path,
                "action": "create" if backup is None else "delete" if content is None else "modify",
                "before_sha256": previous.digest,
                "after_sha256": after.digest,
                "before_artifact_id": backup,
                "mode": mode,
            }
        )
        writes.append(FileWrite(change.path, previous, after))
    artifact = ctx.artifacts_dir / f"{ctx.operation_id}.diff"
    patch = raw.decode()
    metadata: JSONDict = {
        "base_commit": base,
        "patch_sha256": sha256_bytes(raw),
        "paths": [item["path"] for item in files],
        "files": files,
        "diff_artifact_id": str(artifact),
    }

    async def recheck() -> None:
        current = await asyncio.to_thread(git, ctx.workspace.root, "rev-parse", "HEAD")
        if current.decode().strip() != base or any(
            read_target(ctx.workspace, change.path) != change.before for change in writes
        ):
            raise ToolError("PATCH_BASE_CHANGED", "The base or a patch target changed.")

    async def execute() -> JSONDict:
        ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
        backups = []
        for item, change in zip(files, writes, strict=True):
            if change.before.content is not None:
                backup = Path(item["before_artifact_id"])
                write_durable(backup, change.before.content)
                backups.append(str(backup))
        write_durable(artifact, raw)
        write_files(ctx.workspace, writes, conflict="PATCH_BASE_CHANGED")
        diagnostics = []
        for change in writes:
            if change.after.content is not None:
                diagnostic = syntax_diagnostic(
                    ctx, ctx.workspace.root / change.path, change.path, change.after.content
                ).get("syntax_diagnostic")
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
        data = {**metadata, "diff": patch, "workspace_applied": True}
        if diagnostics:
            data["syntax_diagnostics"] = diagnostics
        return {
            "data": data,
            "content": f"Applied {len(files)} file(s).\n{patch}"
            + "".join(syntax_hint(diagnostic) for diagnostic in diagnostics),
            "artifact_ids": (*backups, str(artifact)),
        }

    return PreparedTool({**metadata, "preview": patch}, execute, recheck)


def register_apply_patch(registry: ToolRegistry) -> None:
    registry.register(
        "apply_patch",
        "Apply one version-checked patch to multiple workspace files as a recoverable operation.",
        StagePatchArgs,
        prepare_apply_patch,
    )


def register_staging(registry: ToolRegistry) -> None:
    registry.register(
        "stage_patch",
        "Preview and save a version-checked multi-file patch for review.",
        StagePatchArgs,
        prepare_stage_patch,
    )
