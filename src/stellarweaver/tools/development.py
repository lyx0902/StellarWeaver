"""Typed development actions executed by the configured sandbox."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Literal, get_args

from pydantic import Field, field_validator

from stellarweaver.domain import JSONDict
from stellarweaver.tools.commands import RunCommandArgs, prepare_run_command
from stellarweaver.tools.files import Arguments
from stellarweaver.tools.registry import Prepare, PreparedTool, ToolContext, ToolRegistry
from stellarweaver.tools.workspace import ToolError, Workspace

Action = Literal["run_tests", "discover_tests", "check_syntax", "run_linter", "run_python_snippet"]
_TARGET_ACTIONS: tuple[Literal["run_tests", "discover_tests", "check_syntax", "run_linter"], ...] = (
    "run_tests",
    "discover_tests",
    "check_syntax",
    "run_linter",
)
_ACTIONS: tuple[Action, ...] = get_args(Action)
_SELECTOR_RE = re.compile(r"^[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*$")
_KEYWORD_RE = re.compile(r"^[A-Za-z0-9_ ()]+$")

_WORKER = """\
import ast, importlib.util, json, os, pathlib, runpy, site, sys

action = sys.argv[1]
if os.path.isdir("/deps"):
    site.addsitedir("/deps")
path_entries = [str(pathlib.Path.cwd() / "src"), str(pathlib.Path.cwd())]
if action == "run_python_snippet":
    code = sys.argv[2]
    sys.path[:0] = path_entries
    exec(compile(code, "<snippet>", "exec"), {"__name__": "__main__"})
    raise SystemExit(0)
target, framework = sys.argv[2], sys.argv[3]
selector = sys.argv[4] if len(sys.argv) > 4 else ""
keyword = sys.argv[5] if len(sys.argv) > 5 else ""
path = pathlib.Path(target)
if not path.exists():
    raise FileNotFoundError(target)
if action == "run_tests":
    if framework == "pytest":
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        import pytest
        # Resolve the trusted asynchronous plugin before adding task source to sys.path.
        plugins = []
        if importlib.util.find_spec("pytest_asyncio") is not None:
            from pytest_asyncio import plugin
            plugins.append(plugin)
        sys.path[:0] = path_entries
        node = f"{target}::{selector}" if selector else target
        pytest_argv = ["-q", "-p", "no:cacheprovider"]
        if keyword:
            pytest_argv += ["-k", keyword]
        pytest_argv += ["--", node]
        raise SystemExit(pytest.main(pytest_argv, plugins=plugins))
    else:
        import unittest
        sys.path[:0] = path_entries
        argv = ["unittest", "discover", "-s", target if path.is_dir() else str(path.parent)]
        if path.is_file():
            argv += ["-p", path.name]
        if selector:
            argv += ["-k", selector.split("::")[-1]]
        if keyword:
            argv += ["-k", keyword]
        unittest.main(module=None, argv=argv)
elif action == "run_linter":
    sys.argv = ["ruff", "check", "--no-cache", "--output-format", "concise", "--", target]
    runpy.run_module("ruff", run_name="__main__")
elif action == "check_syntax":
    compile(path.read_bytes(), target, "exec")
    print("Syntax check passed")
else:
    files = [path] if path.is_file() else sorted(
        file for file in path.rglob("*.py")
        if file.name.startswith("test_") or file.name.endswith("_test.py")
    )
    tests, errors = [], []
    for file in files[:200]:
        try:
            if file.stat().st_size > 262144:
                raise ValueError("File exceeds discovery limit")
            tree = ast.parse(file.read_bytes(), filename=str(file))
        except (ValueError, SyntaxError, OSError):
            errors.append(str(file))
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                if len(tests) <= 200:
                    tests.append({"path": str(file), "line": node.lineno, "name": node.name})
    print(json.dumps({"tests": tests[:200], "parse_errors": errors, "truncated": len(files) > 200 or len(tests) > 200}))
"""
_PREFIX = ("python", "-I", "-B", "-c", _WORKER)


class DevelopmentArgs(Arguments):
    target: str = Field(default=".", min_length=1, max_length=240)
    framework: Literal["pytest", "unittest"] = "unittest"
    selector: str | None = Field(default=None, min_length=1, max_length=200)
    keyword: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("target")
    @classmethod
    def relative_target(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or path.as_posix() != value
            or any(
                part.startswith("-")
                or part == ".."
                or any(char in part for char in ("\\", ":", "*", "?", "\0"))
                for part in path.parts
            )
            or Workspace.is_protected(value)
        ):
            raise ValueError("Use a normalized workspace-relative path")
        return value

    @field_validator("selector")
    @classmethod
    def valid_selector(cls, value: str | None) -> str | None:
        if value is not None and not _SELECTOR_RE.fullmatch(value):
            raise ValueError("Use a Python test selector like TestCase::test_name")
        return value

    @field_validator("keyword")
    @classmethod
    def valid_keyword(cls, value: str | None) -> str | None:
        if value is not None:
            stripped = value.strip()
            if not stripped or not _KEYWORD_RE.fullmatch(stripped):
                raise ValueError("Use only letters, digits, underscores, spaces, and parentheses")
            return stripped
        return value


class SnippetArgs(Arguments):
    code: str = Field(min_length=1, max_length=4096)
    timeout_seconds: int = Field(default=30, ge=1, le=60)

    @field_validator("code")
    @classmethod
    def non_empty_code(cls, value: str) -> str:
        if not value.strip() or "\0" in value:
            raise ValueError("Provide a non-empty UTF-8 Python snippet without null bytes")
        return value


def development_argv(action: Action, args: DevelopmentArgs | SnippetArgs) -> list[str]:
    if action == "run_python_snippet":
        if not isinstance(args, SnippetArgs):
            raise ValueError("run_python_snippet requires SnippetArgs")
        return [*_PREFIX, action, args.code]
    if not isinstance(args, DevelopmentArgs):
        raise ValueError(f"{action} requires DevelopmentArgs")
    if action == "run_tests" and (args.selector is not None or args.keyword is not None):
        return [
            *_PREFIX,
            action,
            args.target,
            args.framework,
            args.selector or "",
            args.keyword or "",
        ]
    return [*_PREFIX, action, args.target, args.framework]


def decode_development(argv: list[str]) -> tuple[Action, DevelopmentArgs | SnippetArgs] | None:
    prefix_len = len(_PREFIX)
    if len(argv) < prefix_len + 2 or tuple(argv[:prefix_len]) != _PREFIX:
        return None
    action_raw = argv[prefix_len]
    if action_raw not in _ACTIONS:
        return None
    action = next(item for item in _ACTIONS if item == action_raw)
    tail = argv[prefix_len + 1 :]
    try:
        if action == "run_python_snippet":
            if len(tail) != 1:
                return None
            return action, SnippetArgs.model_validate({"code": tail[0]})
        if len(tail) == 2:
            args = DevelopmentArgs.model_validate({"target": tail[0], "framework": tail[1]})
            return action, args
        if action == "run_tests" and len(tail) == 4:
            args = DevelopmentArgs.model_validate(
                {
                    "target": tail[0],
                    "framework": tail[1],
                    "selector": tail[2] or None,
                    "keyword": tail[3] or None,
                }
            )
            return action, args
    except ValueError:
        return None
    return None


def _prepare(action: Literal["run_tests", "discover_tests", "check_syntax", "run_linter"]) -> Prepare:
    async def prepare(args: DevelopmentArgs, ctx: ToolContext) -> PreparedTool:
        if ctx.command_runner is None:
            raise ToolError(
                "SANDBOX_UNAVAILABLE", "Development tools require an isolated executor."
            )
        if action != "run_tests" and (args.selector is not None or args.keyword is not None):
            raise ToolError(
                "INVALID_ARGUMENTS", "selector and keyword are only supported by run_tests."
            )
        target = ctx.workspace.resolve(args.target, allow_directory=action != "check_syntax")
        if args.selector is not None and target.is_dir() and args.framework == "pytest":
            raise ToolError(
                "INVALID_ARGUMENTS", "pytest selector requires a specific test file target."
            )
        checked = DevelopmentArgs(
            target=ctx.workspace.relative(target),
            framework=args.framework,
            selector=args.selector,
            keyword=args.keyword,
        )
        if target.is_file() and target.suffix != ".py":
            raise ToolError("UNSUPPORTED_TARGET", "Choose a Python source or test file.")
        prepared = await prepare_run_command(
            RunCommandArgs(argv=development_argv(action, checked)), ctx
        )

        async def recheck() -> None:
            ctx.workspace.recheck(args.target, target, allow_directory=action != "check_syntax")
            if prepared.recheck is not None:
                await prepared.recheck()

        async def execute() -> JSONDict:
            await recheck()
            result = await prepared.execute()
            data = result["data"]
            data["diagnostic"] = {
                "action": action,
                "target": checked.target,
                "framework": checked.framework,
                **({"selector": checked.selector} if checked.selector is not None else {}),
                **({"keyword": checked.keyword} if checked.keyword is not None else {}),
                "exit_code": data.get("exit_code"),
                "timed_out": data.get("timed_out", False),
                "output_truncated": any(
                    data.get(key) for key in ("stdout_truncated", "stderr_truncated")
                ),
            }
            return result

        invocation = {
            **prepared.invocation,
            "development_action": action,
            "path": checked.target,
            "framework": checked.framework,
            **({"selector": checked.selector} if checked.selector is not None else {}),
            **({"keyword": checked.keyword} if checked.keyword is not None else {}),
        }
        return replace(prepared, invocation=invocation, execute=execute, recheck=recheck)

    return prepare


async def _prepare_snippet(args: SnippetArgs, ctx: ToolContext) -> PreparedTool:
    if ctx.command_runner is None:
        raise ToolError("SANDBOX_UNAVAILABLE", "Development tools require an isolated executor.")
    prepared = await prepare_run_command(
        RunCommandArgs(
            argv=development_argv("run_python_snippet", args),
            timeout_seconds=args.timeout_seconds,
        ),
        ctx,
    )

    async def execute() -> JSONDict:
        if prepared.recheck is not None:
            await prepared.recheck()
        result = await prepared.execute()
        data = result["data"]
        data["diagnostic"] = {
            "action": "run_python_snippet",
            "target": ".",
            "framework": "python",
            "exit_code": data.get("exit_code"),
            "timed_out": data.get("timed_out", False),
            "output_truncated": any(
                data.get(key) for key in ("stdout_truncated", "stderr_truncated")
            ),
        }
        return result

    invocation = {
        **prepared.invocation,
        "development_action": "run_python_snippet",
        "path": ".",
        "framework": "python",
    }
    return replace(prepared, invocation=invocation, execute=execute)


def register_development_tools(registry: ToolRegistry) -> None:
    descriptions: dict[
        Literal["run_tests", "discover_tests", "check_syntax", "run_linter"], str
    ] = {
        "run_tests": (
            "Run a unittest or pytest file/directory in the isolated snapshot, optionally "
            "filtering with selector (TestCase::test_name) or keyword (-k expression)."
        ),
        "discover_tests": "Find Python test definitions in the sandbox without importing them.",
        "check_syntax": "Compile one Python source file in the sandbox without writing bytecode.",
        "run_linter": "Run the image's Ruff checker on a Python file or directory.",
    }
    for action in _TARGET_ACTIONS:
        registry.register(
            action,
            descriptions[action],
            DevelopmentArgs,
            _prepare(action),
            authorization_tool="run_command",
        )
    registry.register(
        "run_python_snippet",
        (
            "Run a short Python reproduction snippet against the workspace snapshot in the "
            "isolated network-disabled sandbox without writing to the host."
        ),
        SnippetArgs,
        _prepare_snippet,
        authorization_tool="run_command",
    )
