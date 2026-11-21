"""Program-owned permissions; repository instructions cannot grant authority."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from stellarweaver.policy.risk import DS_SEARCH as DS_SEARCH
from stellarweaver.policy.risk import POLICY_VERSION, assess, fingerprint

READ_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search_text",
        "git_status",
        "git_diff",
        "load_skill",
        "find_symbol",
        "find_references",
        "find_related_tests",
        DS_SEARCH,
    }
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
ApprovalCheck = Callable[[], Awaitable[None]]


class PolicyEngine:
    def __init__(
        self,
        preset: Literal["ask", "read-only", "workspace-edit"] = "ask",
        *,
        workspace: Path | None = None,
        interactive: bool = False,
        approval: Approval | None = None,
        policy_file: Path | None = None,
        allow_sandbox_development: bool = False,
    ) -> None:
        if preset not in {"ask", "read-only", "workspace-edit"}:
            raise ValueError("Unknown policy preset")
        self.preset = preset
        self.workspace = workspace.resolve() if workspace else None
        self.interactive = interactive
        self.approval = approval
        self.allow_sandbox_development = allow_sandbox_development
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
                    "registered_tool",
                    "executor",
                    "command_boundary",
                    "policy_binding",
                    "hooks_binding",
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
                if invocation.get("server_id") == grant.server_id and fingerprint(
                    arguments
                ) == fingerprint(grant.arguments):
                    return True
        return False

    def binding(self) -> str:
        return fingerprint(
            {
                "version": POLICY_VERSION,
                "workspace": str(self.workspace),
                "preset": self.preset,
                "interactive": self.interactive,
                "allow_sandbox_development": self.allow_sandbox_development,
                "grants": [asdict(grant) for grant in self._grants],
            }
        )

    def prohibition(self, invocation: dict[str, Any]) -> str | None:
        tool = invocation["tool"]
        if self.workspace is not None and Path(invocation["workspace"]) != self.workspace:
            return "Workspace does not match frozen policy"
        if assess(invocation).recommendation == "deny":
            return "Operation is prohibited by the capability boundary"
        if self.preset != "read-only" or tool in READ_TOOLS:
            return None
        if tool == "mcp_start" and self._matches(invocation):
            return None
        if invocation.get("remote") and invocation.get("confirmed_read_only"):
            return None
        return "read-only policy forbids this tool"

    async def decide(
        self,
        invocation: dict[str, Any],
        *,
        force_approval: bool = False,
        before_approval: ApprovalCheck | None = None,
    ) -> dict[str, Any]:
        tool = invocation["tool"]
        blocked = self.prohibition(invocation)
        if blocked:
            return {"decision": "deny", "reason": blocked}
        # Model-authored queries cross a separate boundary even in read-only mode.
        if tool == DS_SEARCH:
            return await self._ask(invocation, one_shot=True, check=before_approval)
        if tool in READ_TOOLS and not force_approval:
            return {"decision": "allow", "reason": "Built-in workspace read"}
        if (
            not force_approval
            and self.allow_sandbox_development
            and tool == "run_command"
            and invocation.get("executor") == "configured"
            and invocation.get("development_action") == invocation.get("registered_tool")
            and invocation.get("development_action") is not None
        ):
            return {"decision": "allow", "reason": "Frozen benchmark development scope"}
        scope = self._scope(invocation)
        one_shot = force_approval or assess(invocation).level == "L2"
        if not force_approval and (
            self._matches(invocation) or (not one_shot and scope in self._session)
        ):
            return {"decision": "allow", "reason": "Explicit scoped grant"}
        if not force_approval and self.preset == "workspace-edit" and tool in EDIT_TOOLS:
            return {"decision": "allow", "reason": "workspace-edit allows built-in edits"}
        return await self._ask(invocation, one_shot=one_shot, check=before_approval)

    async def _ask(
        self, invocation: dict[str, Any], *, one_shot: bool, check: ApprovalCheck | None
    ) -> dict[str, Any]:
        approval = self.approval
        if not self.interactive or approval is None:
            return {
                "decision": "blocked",
                "reason": "Approval required; no interactive TTY or matching grant",
            }
        if check is not None:
            await check()
        answer = await approval({**deepcopy(invocation), "one_shot": one_shot})
        if not one_shot and answer in {"session", "allow-session"}:
            self._session.add(self._scope(invocation))
            return {"decision": "allow", "reason": "User allowed this session scope"}
        if answer in {"once", "allow", "allow-once"}:
            return {"decision": "allow", "reason": "User allowed this invocation"}
        return {"decision": "deny", "reason": "User denied this invocation"}
