"""Read-only Git queries with external programs and optional locks disabled."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from stellarweaver.tools.commands import execute_command
from stellarweaver.tools.workspace import ToolError

if TYPE_CHECKING:
    from stellarweaver.domain import JSONDict
    from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry


class GitStatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GitDiffArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    paths: list[str] = Field(default_factory=list, max_length=256)
    staged: bool = False


def _git_prefix() -> list[str]:
    return [
        "git",
        "--no-pager",
        "--literal-pathspecs",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.pager=cat",
        "-c",
        "core.quotePath=false",
        "-c",
        "diff.external=",
        "-c",
        "core.attributesFile=" + os.devnull,
    ]


async def _git(args: list[str], context: ToolContext) -> JSONDict:
    environment = {
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_NO_LAZY_FETCH": "1",
    }
    # Even status/diff can run clean/process filters selected by attributes.
    # Read only their names and override all drivers without executing config
    # values. Preserve unrelated settings such as core.autocrlf.
    configuration = await execute_command(
        _git_prefix() + ["config", "--null", "--name-only", "--get-regexp", r"^filter\."],
        context.workspace.root,
        config=context.execution,
        artifacts_dir=context.artifacts_dir,
        environment_overrides=environment,
    )
    data = configuration["data"]
    # `git config --get-regexp` exits 1 when no filters exist. Only that
    # ordinary nonzero result may continue: containment/cleanup/cancellation
    # failures can also have exit 0 or 1 and must retain their original state.
    no_filters = (
        configuration["status"] == "error"
        and data["exit_code"] == 1
        and data.get("code") == "COMMAND_FAILED"
        and not any(data.get(key) for key in ("timed_out", "cancelled", "output_incomplete"))
    )
    if configuration["status"] != "ok" and not no_filters:
        return configuration
    if configuration["data"]["stdout_truncated"]:
        raise ToolError(
            "GIT_CONFIG_TOO_LARGE", "Git filter configuration exceeds the safe enumeration limit."
        )
    drivers: set[str] = set()
    for key in configuration["data"]["stdout"].split("\x00"):
        if key.startswith("filter.") and key.count(".") >= 2:
            driver = key[len("filter.") :].rsplit(".", 1)[0]
            if "=" in driver or "\n" in driver:
                raise ToolError(
                    "GIT_UNSAFE_CONFIG", "A Git filter name cannot be safely overridden."
                )
            drivers.add(driver)
    overrides: list[str] = []
    for driver in sorted(drivers):
        for setting in ("clean", "smudge", "process"):
            overrides.extend(["-c", f"filter.{driver}.{setting}="])
        overrides.extend(["-c", f"filter.{driver}.required=false"])
    result = await execute_command(
        _git_prefix() + overrides + args,
        context.workspace.root,
        config=context.execution,
        artifacts_dir=context.artifacts_dir,
        environment_overrides=environment,
    )
    if result["status"] == "error":
        result["data"]["code"] = "GIT_ERROR"
    return result


def _parse_status(raw: str) -> JSONDict:
    entries: list[JSONDict] = []
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    fields = iter(raw.split("\x00"))
    for field in fields:
        if not field:
            continue
        if len(field) < 4 or field[2] != " ":
            raise ToolError(
                "GIT_STATUS_INVALID", "Git returned an incomplete porcelain status record."
            )
        index, worktree, path = field[0], field[1], field[3:]
        entry: JSONDict = {"path": path, "index_status": index, "worktree_status": worktree}
        if index in "RC" or worktree in "RC":
            entry["original_path"] = next(fields, "")
        entries.append(entry)
        if index == "?":
            untracked.append(path)
        elif index != "!":
            if index != " ":
                staged.append(path)
            if worktree != " ":
                unstaged.append(path)
    return {"staged": staged, "unstaged": unstaged, "untracked": untracked, "entries": entries}


async def prepare_git_status(args: GitStatusArgs, context: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    async def execute() -> JSONDict:
        result = await _git(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=all"],
            context,
        )
        if result["status"] == "ok":
            if result["data"]["stdout_truncated"]:
                # A head/tail stream cannot safely be parsed as whole records.
                result["data"].update(
                    {
                        "staged": [],
                        "unstaged": [],
                        "untracked": [],
                        "entries": [],
                        "status_incomplete": True,
                    }
                )
            else:
                result["data"].update(_parse_status(result["data"]["stdout"]))
                result["data"]["status_incomplete"] = False
            result["content"] = str(
                {
                    key: result["data"][key]
                    for key in ("staged", "unstaged", "untracked", "status_incomplete")
                }
            )
        return result

    return PreparedTool({}, execute)


async def prepare_git_diff(args: GitDiffArgs, context: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    paths = [
        context.workspace.relative(
            context.workspace.resolve(path, allow_directory=True, must_exist=False)
        )
        for path in args.paths
    ]

    async def recheck() -> None:
        for original, normalized in zip(args.paths, paths, strict=True):
            if (
                context.workspace.relative(
                    context.workspace.resolve(original, allow_directory=True, must_exist=False)
                )
                != normalized
            ):
                raise ToolError("PATH_CHANGED", "A diff path changed after authorization.")

    async def execute() -> JSONDict:
        argv = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--ignore-submodules=all"]
        if args.staged:
            argv.append("--cached")
        listing = await _git(argv + ["--name-only", "-z", "--"] + paths, context)
        if listing["status"] != "ok":
            return listing
        if listing["data"]["stdout_truncated"]:
            raise ToolError(
                "OUTPUT_TRUNCATED",
                "Changed-path enumeration exceeded the output limit; request narrower diff paths.",
            )
        selected: list[str] = []
        omitted: list[str] = []
        for path in listing["data"]["stdout"].split("\x00"):
            if not path:
                continue
            try:
                context.workspace.resolve(path, must_exist=False)
            except ToolError:
                omitted.append(path)
            else:
                selected.append(path)
        if selected:
            result = await _git(argv + ["--"] + selected, context)
        else:
            result = {
                "status": "ok",
                "content": "No diff.",
                "artifact_ids": (),
                "data": {"exit_code": 0, "stdout": "", "stderr": "", "stdout_truncated": False},
            }
        result["data"]["diff"] = result["data"]["stdout"]
        result["data"]["staged"] = args.staged
        result["data"]["paths"] = paths
        result["data"]["omitted_protected_paths"] = omitted
        if result["status"] == "ok":
            result["content"] = result["data"]["diff"] or "No diff."
        return result

    return PreparedTool({"paths": paths, "staged": args.staged}, execute, recheck)


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        "git_status",
        "Read staged, unstaged and untracked Git paths.",
        GitStatusArgs,
        prepare_git_status,
    )
    registry.register(
        "git_diff",
        "Read Git diff without external diff drivers or text conversion.",
        GitDiffArgs,
        prepare_git_diff,
    )
