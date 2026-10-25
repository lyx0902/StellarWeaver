"""Program-owned permissions; repository instructions cannot grant authority."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

READ_TOOLS = frozenset(
    {"list_files", "read_file", "search_text", "git_status", "git_diff", "load_skill"}
)
EDIT_TOOLS = frozenset({"create_file", "edit_file"})


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tool: str
    paths: tuple[str, ...] = ()
    argv: tuple[str, ...] | None = None
    cwd: str | None = None
    server_id: str | None = None
    arguments: dict[str, Any] | None = None


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    workspace: str
    grants: tuple[Grant, ...] = Field(default_factory=tuple)


@dataclass(frozen=True)
class FrozenGrant:
    tool: str
    paths: tuple[str, ...]
    argv: tuple[str, ...] | None
    cwd: str | None
    server_id: str | None = None
    arguments: dict[str, Any] | None = None


Approval = Callable[[dict[str, Any]], Awaitable[str]]


class PolicyEngine:
    def __init__(
        self,
        preset: Literal["ask", "read-only", "workspace-edit"] = "ask",
        *,
        workspace: Path | None = None,
        interactive: bool = False,
        approval: Approval | None = None,
        policy_file: Path | None = None,
    ) -> None:
        if preset not in {"ask", "read-only", "workspace-edit"}:
            raise ValueError("Unknown policy preset")
        self.preset = preset
        self.workspace = workspace.resolve() if workspace else None
        self.interactive = interactive
        self.approval = approval
        self._grants: tuple[FrozenGrant, ...] = ()
        self._session: set[str] = set()
        if policy_file is not None:
            if self.workspace is None:
                raise ValueError("Policy files require an explicit workspace")
            config = PolicyFile.model_validate_json(policy_file.read_text(encoding="utf-8"))
            given_root = Path(config.workspace)
            if not given_root.is_absolute() or given_root.resolve() != self.workspace:
                raise ValueError("Policy workspace must be the matching absolute workspace")
            grants: list[FrozenGrant] = []
            for grant in config.grants:
                if grant.tool in {"run_command", "mcp_start"}:
                    if not grant.argv or grant.cwd is None:
                        raise ValueError(f"{grant.tool} grant requires exact argv and cwd")
                    if grant.tool == "mcp_start" and not grant.server_id:
                        raise ValueError("mcp_start grant requires exact server_id")
                    cwd = Path(grant.cwd)
                    if cwd.is_absolute() or ".." in cwd.parts:
                        raise ValueError("Granted cwd must stay inside workspace")
                elif grant.tool in EDIT_TOOLS:
                    if not grant.paths:
                        raise ValueError("File edit grants require explicit paths")
                    for path in grant.paths:
                        if Path(path).is_absolute() or ".." in Path(path).parts:
                            raise ValueError("Granted paths must stay inside workspace")
                elif grant.tool.startswith("mcp__"):
                    if not grant.server_id or grant.arguments is None:
                        raise ValueError("MCP grants require exact server_id and arguments")
                elif grant.tool not in READ_TOOLS:
                    raise ValueError(f"Unknown policy tool: {grant.tool}")
                grants.append(
                    FrozenGrant(
                        grant.tool,
                        grant.paths,
                        grant.argv,
                        grant.cwd,
                        grant.server_id,
                        grant.arguments,
                    )
                )
            self._grants = tuple(grants)

    @staticmethod
    def _scope(invocation: dict[str, Any]) -> str:
        return json.dumps(
            {
                key: invocation.get(key)
                for key in (
                    "tool",
                    "workspace",
                    "path",
                    "argv",
                    "cwd",
                    "server_id",
                    "arguments_json",
                )
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def _matches(self, invocation: dict[str, Any]) -> bool:
        for grant in self._grants:
            if grant.tool != invocation["tool"]:
                continue
            if grant.tool in {"run_command", "mcp_start"}:
                if (
                    tuple(invocation.get("argv", ())) == grant.argv
                    and invocation.get("cwd") == grant.cwd
                    and (
                        grant.tool != "mcp_start" or invocation.get("server_id") == grant.server_id
                    )
                ):
                    return True
            elif grant.tool in EDIT_TOOLS:
                if invocation.get("path") in grant.paths:
                    return True
            elif grant.tool in READ_TOOLS:
                return True
            elif grant.tool.startswith("mcp__"):
                try:
                    arguments = json.loads(invocation.get("arguments_json", "null"))
                except (ValueError, TypeError):
                    continue
                if invocation.get("server_id") == grant.server_id and arguments == grant.arguments:
                    return True
        return False

    async def decide(self, invocation: dict[str, Any]) -> dict[str, Any]:
        tool = invocation["tool"]
        if self.workspace is not None and Path(invocation["workspace"]) != self.workspace:
            return {"decision": "deny", "reason": "Workspace does not match frozen policy"}
        if tool in READ_TOOLS:
            return {"decision": "allow", "reason": "Built-in workspace read"}
        if invocation.get("remote") and invocation.get("confirmed_read_only"):
            return {
                "decision": "allow",
                "reason": "User configuration confirms this MCP tool is read-only",
            }
        # Starting a server has its own exact program grant even in plan mode.
        if tool == "mcp_start" and self._matches(invocation):
            return {
                "decision": "allow",
                "reason": "Explicit server startup argv/cwd/server_id grant",
            }
        if self.preset == "read-only":
            return {"decision": "deny", "reason": "read-only policy forbids this tool"}
        scope = self._scope(invocation)
        if self._matches(invocation) or scope in self._session:
            return {"decision": "allow", "reason": "Explicit scoped grant"}
        if self.preset == "workspace-edit" and tool in EDIT_TOOLS:
            return {"decision": "allow", "reason": "workspace-edit allows built-in edits"}
        if not self.interactive or self.approval is None:
            return {
                "decision": "blocked",
                "reason": "Approval required; no interactive TTY or matching grant",
            }
        answer = await self.approval(invocation)
        if answer in {"session", "allow-session"}:
            self._session.add(scope)
            return {"decision": "allow", "reason": "User allowed this session scope"}
        if answer in {"once", "allow", "allow-once"}:
            return {"decision": "allow", "reason": "User allowed this invocation"}
        return {"decision": "deny", "reason": "User denied this invocation"}
