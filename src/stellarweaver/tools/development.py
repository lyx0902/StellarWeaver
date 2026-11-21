"""Typed development actions executed by the configured sandbox."""

from __future__ import annotations

from dataclasses import replace
from pathlib import PurePosixPath
from typing import Literal, get_args

from pydantic import Field, field_validator

from stellarweaver.domain import JSONDict
from stellarweaver.tools.commands import RunCommandArgs, prepare_run_command
from stellarweaver.tools.files import Arguments
from stellarweaver.tools.registry import Prepare, PreparedTool, ToolContext, ToolRegistry
from stellarweaver.tools.workspace import ToolError, Workspace

Action = Literal["run_tests", "discover_tests", "check_syntax", "run_linter"]
_ACTIONS: tuple[Action, ...] = get_args(Action)
_WORKER = """\
import ast, importlib.util, json, os, pathlib, runpy, sys

action, target, framework = sys.argv[1:]
path = pathlib.Path(target)
if not path.exists():
    raise FileNotFoundError(target)
sys.path.append("/deps")
if action == "run_tests":
    path_entries = [str(pathlib.Path.cwd() / "src"), str(pathlib.Path.cwd())]
    if framework == "pytest":
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        import pytest
        # Resolve the trusted asynchronous plugin before adding task source to sys.path.
        plugins = []
        if importlib.util.find_spec("pytest_asyncio") is not None:
            from pytest_asyncio import plugin
            plugins.append(plugin)
        sys.path[:0] = path_entries
        raise SystemExit(pytest.main(["-q", "-p", "no:cacheprovider", "--", target], plugins=plugins))
    else:
        import unittest
        sys.path[:0] = path_entries
        argv = ["unittest", "discover", "-s", target if path.is_dir() else str(path.parent)]
        if path.is_file():
            argv += ["-p", path.name]
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


def development_argv(action: Action, args: DevelopmentArgs) -> list[str]:
    return [*_PREFIX, action, args.target, args.framework]


def decode_development(argv: list[str]) -> tuple[Action, DevelopmentArgs] | None:
    if len(argv) != len(_PREFIX) + 3 or tuple(argv[:-3]) != _PREFIX or argv[-3] not in _ACTIONS:
        return None
    try:
        action = next(action for action in _ACTIONS if action == argv[-3])
        args = DevelopmentArgs.model_validate({"target": argv[-2], "framework": argv[-1]})
        return action, args
    except ValueError:
        return None


def _prepare(action: Action) -> Prepare:
    async def prepare(args: DevelopmentArgs, ctx: ToolContext) -> PreparedTool:
        if ctx.command_runner is None:
            raise ToolError(
                "SANDBOX_UNAVAILABLE", "Development tools require an isolated executor."
            )
        target = ctx.workspace.resolve(args.target, allow_directory=action != "check_syntax")
        checked = DevelopmentArgs(target=ctx.workspace.relative(target), framework=args.framework)
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
        }
        return replace(prepared, invocation=invocation, execute=execute, recheck=recheck)

    return prepare


def register_development_tools(registry: ToolRegistry) -> None:
    descriptions = {
        "run_tests": "Run a unittest or pytest file/directory in the isolated snapshot.",
        "discover_tests": "Find Python test definitions in the sandbox without importing them.",
        "check_syntax": "Compile one Python source file in the sandbox without writing bytecode.",
        "run_linter": "Run the image's Ruff checker on a Python file or directory.",
    }
    for action in _ACTIONS:
        registry.register(
            action,
            descriptions[action],
            DevelopmentArgs,
            _prepare(action),
            authorization_tool="run_command",
        )
