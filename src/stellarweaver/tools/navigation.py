"""Expose bounded Python navigation through the shared registry."""

import asyncio
from typing import get_args

from pydantic import Field

from stellarweaver.context.navigation import NavigationIndex, NavigationMode
from stellarweaver.context.repo_map import RepoMap
from stellarweaver.domain import JSONDict
from stellarweaver.tools.files import Arguments
from stellarweaver.tools.registry import Prepare, PreparedTool, ToolContext, ToolRegistry


class NavigationArgs(Arguments):
    symbol: str = Field(pattern=r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", max_length=160)
    path: str | None = None
    limit: int = Field(default=10, ge=1, le=50)


def register_navigation(registry: ToolRegistry) -> None:
    index = NavigationIndex(RepoMap(registry.workspace))

    def bind(mode: NavigationMode) -> Prepare:
        async def prepare(args: NavigationArgs, ctx: ToolContext) -> PreparedTool:
            path = ctx.workspace.resolve(args.path) if args.path is not None else None
            hint = ctx.workspace.relative(path) if path is not None else None

            async def execute() -> JSONDict:
                if path is not None and args.path is not None:
                    ctx.workspace.recheck(args.path, path)
                data = await asyncio.to_thread(index.query, mode, args.symbol, hint, args.limit)
                return {"data": data}

            return PreparedTool({"path": hint, "symbol": args.symbol}, execute)

        return prepare

    descriptions = {
        "find_symbol": "Find Python definitions and import bindings by qualified or simple name.",
        "find_references": "Find static Python references with file, line, and content hash.",
        "find_related_tests": "Rank Python test files referencing a symbol; dynamic links are unknown.",
    }
    for mode in get_args(NavigationMode):
        registry.register(mode, descriptions[mode], NavigationArgs, bind(mode))
