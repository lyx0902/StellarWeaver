"""Bounded rg search with a deterministic Python fallback, never a shell."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from stellarweaver.tools.files import Arguments
from stellarweaver.tools.workspace import SKIP_DIRECTORIES, ToolError

if TYPE_CHECKING:
    from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry


class SearchTextArgs(Arguments):
    pattern: str = Field(min_length=1, max_length=1000)
    path: str = "."
    glob: str | None = None
    max_matches: int = Field(default=100, ge=1, le=1000)


def _paths(root: Path, ctx: ToolContext) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for directory, folders, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        folders[:] = sorted(
            folder
            for folder in folders
            if not ctx.workspace.is_ignored(ctx.workspace.relative(parent / folder), is_dir=True)
            and not (parent / folder).is_symlink()
        )
        for filename in sorted(files):
            candidate = parent / filename
            relative = ctx.workspace.relative(candidate)
            if not candidate.is_symlink() and not ctx.workspace.is_ignored(relative):
                yield candidate


def _result(
    matches: list[dict[str, Any]], truncated: bool, backend: str, skipped: list[str] | None = None
) -> dict[str, Any]:
    return {
        "content": "\n".join(
            f"{match['path']}:{match['line']}: {match['text']}" for match in matches
        ),
        "data": {
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
            "skipped_files": (skipped or [])[:100],
            "backend": backend,
        },
    }


async def _rg_search(
    executable: str, args: SearchTextArgs, ctx: ToolContext, root: Path
) -> dict[str, Any]:
    from stellarweaver.tools.commands import execute_command

    argv = [
        executable,
        "--json",
        "--line-number",
        "--with-filename",
        "--color",
        "never",
        "--hidden",
        "--no-require-git",
        "--max-count",
        str(args.max_matches + 1),
    ]
    if args.glob is not None:
        argv.extend(["--glob", args.glob])
    # Security exclusions come after user filters; all remain active for explicit roots.
    excluded = sorted(
        SKIP_DIRECTORIES
        | {
            ".aws",
            ".ssh",
            ".env",
            ".env.*",
            "credentials",
            "credentials.json",
            "session.db",
            "sessions.db",
            "*.pem",
            "*.key",
            "*.p12",
            "*.pfx",
            "*.sqlite",
            "*.sqlite3",
            "*.sqlite-wal",
            "*.sqlite-shm",
            "*.sqlite3-wal",
            "*.sqlite3-shm",
            "id_rsa*",
            "id_ed25519*",
        }
    )
    for pattern in excluded:
        argv.extend(["--glob", f"!{pattern}"])
    for protected in ctx.workspace.protected_roots:
        if protected.is_relative_to(ctx.workspace.root):
            relative = ctx.workspace.relative(protected)
            argv.extend(["--glob", f"!{relative}", "--glob", f"!{relative}/**"])
    ignore_file = ctx.workspace.root / ".stellarweaverignore"
    if ignore_file.is_file() and not ignore_file.is_symlink():
        argv.extend(["--ignore-file", str(ignore_file)])
    argv.extend(["--", args.pattern, str(root)])
    timeout = min(15.0, float(getattr(ctx.execution, "max_timeout_seconds", 300)))
    outcome = await execute_command(
        argv, ctx.workspace.root, config=ctx.execution, timeout_seconds=timeout
    )
    data = outcome["data"]
    # rg exits 1 for no matches. A normal exit cannot prove cleanup: retain
    # all other terminal execution evidence rather than reporting success.
    no_matches = (
        outcome["status"] == "error"
        and data["exit_code"] == 1
        and data.get("code") == "COMMAND_FAILED"
        and not any(data.get(key) for key in ("timed_out", "cancelled", "output_incomplete"))
    )
    if outcome["status"] != "ok" and not no_matches:
        return outcome
    if data["cancelled"] or data["timed_out"] or data["exit_code"] not in {0, 1}:
        raise ToolError(
            data.get("code", "SEARCH_FAILED"),
            "rg search did not complete.",
            {"exit_code": data["exit_code"], "stderr": data["stderr"]},
        )
    matches: list[dict[str, Any]] = []
    truncated = bool(data["stdout_truncated"])
    for raw in data["stdout"].splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            truncated = True
            continue
        if event.get("type") != "match":
            continue
        match = event["data"]
        value = match["path"].get("text")
        text = match["lines"].get("text")
        if value is None or text is None:
            continue
        candidate = Path(value)
        try:
            relative = ctx.workspace.relative(candidate)
            ctx.workspace.resolve(relative)
        except (ValueError, ToolError):
            continue
        if ctx.workspace.is_ignored(relative):
            continue
        if len(matches) >= args.max_matches:
            truncated = True
            break
        text = text.rstrip("\r\n")
        if len(text) > 4000:
            truncated = True
        matches.append({"path": relative, "line": match["line_number"], "text": text[:4000]})
    return _result(matches, truncated, "rg")


async def prepare_search_text(args: SearchTextArgs, ctx: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    root = ctx.workspace.resolve(args.path, allow_directory=True)
    try:
        expression = re.compile(args.pattern)
    except re.error as exc:
        raise ToolError("INVALID_ARGUMENTS", f"Invalid regular expression: {exc}") from exc

    async def execute() -> dict[str, Any]:
        executable = shutil.which("rg")
        if executable is not None:
            return await _rg_search(executable, args, ctx, root)
        matches: list[dict[str, Any]] = []
        skipped: list[str] = []
        truncated = False
        for candidate in _paths(root, ctx):
            relative = ctx.workspace.relative(candidate)
            if args.glob is not None and not fnmatch.fnmatch(relative, args.glob):
                continue
            try:
                with candidate.open("r", encoding="utf-8", newline="") as stream:
                    number = 0
                    while text := stream.readline(65537):
                        number += 1
                        clipped = len(text) > 65536 and not text.endswith("\n")
                        if clipped:
                            truncated = True
                            while remainder := stream.readline(65537):
                                if remainder.endswith("\n"):
                                    break
                        if "\0" in text:
                            break
                        if expression.search(text):
                            if len(matches) >= args.max_matches:
                                truncated = True
                                break
                            matches.append(
                                {
                                    "path": relative,
                                    "line": number,
                                    "text": text.rstrip("\r\n")[:4000],
                                }
                            )
                            if len(text.rstrip("\r\n")) > 4000:
                                truncated = True
            except (UnicodeDecodeError, OSError):
                skipped.append(relative)
            if len(matches) >= args.max_matches:
                truncated = True
                break
        return _result(matches, truncated, "python", skipped)

    return PreparedTool({"path": ctx.workspace.relative(root)}, execute)


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        "search_text",
        "Search UTF-8 workspace files using a regular expression; respect ignore rules.",
        SearchTextArgs,
        prepare_search_text,
    )
