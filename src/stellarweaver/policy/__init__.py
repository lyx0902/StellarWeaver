"""Program-owned permissions; repository instructions cannot grant authority."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stellarweaver.config import PolicyPreset
from stellarweaver.policy.risk import DS_SEARCH as DS_SEARCH
from stellarweaver.policy.risk import POLICY_VERSION, assess, fingerprint

READ_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search_text",
        "git_status",
        "git_diff",
        "git_log",
        "git_show",
        "load_skill",
        "find_symbol",
        "find_references",
        "find_related_tests",
        "delegate_exploration",
        DS_SEARCH,
    }
)
EDIT_TOOLS = frozenset({"create_file", "edit_file", "apply_patch"})
BENCHMARK_TOOLS = (READ_TOOLS - {DS_SEARCH}) | EDIT_TOOLS
DEVELOPMENT_TOOLS = frozenset(
    {"run_tests", "discover_tests", "check_syntax", "run_linter", "run_python_snippet"}
)


class Grant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    tool: str
    paths: tuple[str, ...] = ()
    argv: tuple[str, ...] | None = None
    cwd: str | None = None
    server_id: str | None = None
    arguments: dict[str, Any] | None = None
    command_boundary: str | None = Field(default=None, min_length=1)
    framework: str | None = None


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
    command_boundary: str | None = None
    framework: str | None = None


Approval = Callable[[dict[str, Any]], Awaitable[str]]
ApprovalCheck = Callable[[], Awaitable[None]]


class PolicyEngine:
    def __init__(
        self,
        preset: PolicyPreset = "ask",
        *,
        workspace: Path | None = None,
        interactive: bool = False,
        approval: Approval | None = None,
        policy_file: Path | None = None,
        allow_sandbox_development: bool = False,
        scoped_test_grants: bool = False,
        allow_benchmark_staging: bool = False,
    ) -> None:
        if preset not in {"ask", "read-only", "workspace-edit", "auto", "benchmark"}:
            raise ValueError("Unknown policy preset")
        self.preset = preset
        self.workspace = workspace.resolve() if workspace else None
        self.interactive = interactive
        self.approval = approval
        self.allow_sandbox_development = allow_sandbox_development
        self.scoped_test_grants = scoped_test_grants
        self.benchmark_tools = (
            BENCHMARK_TOOLS | {"stage_patch"} if allow_benchmark_staging else BENCHMARK_TOOLS
        )
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
                elif grant.tool in DEVELOPMENT_TOOLS:
                    if (
                        not self.scoped_test_grants
                        or not grant.command_boundary
                        or grant.framework not in {"pytest", "unittest"}
                        or grant.paths
                        or grant.argv is not None
                        or grant.cwd is not None
                        or grant.server_id is not None
                        or grant.arguments is not None
                    ):
                        raise ValueError(
                            "Development grants require scoped_test_grants, a command boundary and framework"
                        )
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
                        grant.command_boundary,
                        grant.framework,
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
            if grant.tool == "apply_patch" == invocation["tool"]:
                paths = invocation.get("paths")
                if isinstance(paths, list) and paths and all(path in grant.paths for path in paths):
                    return True
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
                "grants": [
                    {
                        key: value
                        for key, value in asdict(grant).items()
                        if key not in {"command_boundary", "framework"} or value is not None
                    }
                    for grant in self._grants
                ],
                **({"scoped_test_grants": True} if self.scoped_test_grants else {}),
                **({"benchmark_staging": True} if "stage_patch" in self.benchmark_tools else {}),
            }
        )

    @staticmethod
    def _sandbox_development(invocation: dict[str, Any]) -> bool:
        return (
            invocation["tool"] == "run_command"
            and invocation.get("executor") in {"configured", "sandbox"}
            and invocation.get("development_action") is not None
            and invocation.get("development_action") == invocation.get("registered_tool")
        )

    def _development_scope(self, invocation: dict[str, Any]) -> str | None:
        boundary = invocation.get("command_boundary")
        if (
            not self.scoped_test_grants
            or not self._sandbox_development(invocation)
            or not isinstance(boundary, str)
            or not boundary
            or invocation.get("cwd") != "."
            or invocation.get("development_action") not in DEVELOPMENT_TOOLS
        ):
            return None
        from stellarweaver.tools.development import DevelopmentArgs, SnippetArgs, decode_development

        argv = invocation.get("argv")
        decoded = decode_development(argv) if isinstance(argv, list) else None
        if decoded is None:
            return None
        action, args = decoded
        if action != invocation["development_action"]:
            return None
        if isinstance(args, SnippetArgs):
            if invocation.get("path") != "." or invocation.get("framework") != "python":
                return None
        else:
            assert isinstance(args, DevelopmentArgs)
            if args.target != invocation.get("path") or args.framework != invocation.get(
                "framework"
            ):
                return None
        # Only target is omitted. The transport, interpreter, action and framework remain bound.
        return "development:" + fingerprint(
            {
                key: invocation.get(key)
                for key in (
                    "workspace",
                    "registered_tool",
                    "executor",
                    "command_boundary",
                    "policy_binding",
                    "hooks_binding",
                    "cwd",
                    "framework",
                    "timeout_seconds",
                )
            }
        )

    def restore_test_grants(self, decisions: list[dict[str, Any]]) -> None:
        for decision in decisions:
            authorized = {**decision, "tool": "run_command"}
            if (
                decision.get("session_test_grant") is True
                and decision.get("authorization_tool") == "run_command"
                and decision.get("decision") == "allow"
                and decision.get("policy_binding") == self.binding()
                and decision.get("workspace") == str(self.workspace)
                and self.prohibition(authorized) is None
                and (scope := self._development_scope(authorized)) is not None
            ):
                self._session.add(scope)

    def prohibition(self, invocation: dict[str, Any]) -> str | None:
        tool = invocation["tool"]
        if self.workspace is not None and Path(invocation["workspace"]) != self.workspace:
            return "Workspace does not match frozen policy"
        if assess(invocation).recommendation == "deny":
            return "Operation is prohibited by the capability boundary"
        if self.preset == "benchmark":
            permitted = not invocation.get("remote") and (
                tool in self.benchmark_tools
                or (
                    tool == "run_command"
                    and invocation.get("executor") == "sandbox"
                    and invocation.get("registered_tool") != "run_command"
                    and (
                        (invocation.get("sandbox_command_configured") and self._matches(invocation))
                        or (
                            self.allow_sandbox_development and self._sandbox_development(invocation)
                        )
                    )
                )
            )
            return None if permitted else "Operation is outside the frozen benchmark policy"
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
        if self.preset == "benchmark" and force_approval:
            return {
                "decision": "blocked",
                "reason": "Benchmark cannot request interactive approval",
            }
        if tool in READ_TOOLS and not force_approval:
            return {
                "decision": "allow",
                "reason": "Public web search" if tool == DS_SEARCH else "Built-in workspace read",
            }
        if self.preset == "benchmark":
            return {"decision": "allow", "reason": "Frozen benchmark policy"}
        test_scope = None if force_approval else self._development_scope(invocation)
        if test_scope and (
            test_scope in self._session
            or any(
                grant.tool == invocation["registered_tool"]
                and grant.command_boundary == invocation["command_boundary"]
                and grant.framework == invocation["framework"]
                for grant in self._grants
            )
        ):
            return {"decision": "allow", "reason": "Explicit scoped sandbox development grant"}
        if (
            not force_approval
            and self.allow_sandbox_development
            and self._sandbox_development(invocation)
        ):
            return {"decision": "allow", "reason": "Configured benchmark development scope"}
        if self.preset == "auto":
            if not force_approval and (
                tool in EDIT_TOOLS
                or (
                    tool == "run_command"
                    and invocation.get("executor") == "sandbox"
                    and (
                        invocation.get("sandbox_command_configured")
                        or self._sandbox_development(invocation)
                    )
                )
            ):
                return {
                    "decision": "allow",
                    "reason": "Auto allows scoped editing and sandbox checks",
                }
            return await self._ask(
                invocation,
                one_shot=test_scope is None,
                check=before_approval,
                test_scope=test_scope,
            )
        scope = self._scope(invocation)
        one_shot = force_approval or assess(invocation).level == "L2"
        if not force_approval and (
            self._matches(invocation) or (not one_shot and scope in self._session)
        ):
            return {"decision": "allow", "reason": "Explicit scoped grant"}
        if not force_approval and self.preset == "workspace-edit" and tool in EDIT_TOOLS:
            return {"decision": "allow", "reason": "workspace-edit allows built-in edits"}
        return await self._ask(
            invocation, one_shot=one_shot, check=before_approval, test_scope=test_scope
        )

    async def _ask(
        self,
        invocation: dict[str, Any],
        *,
        one_shot: bool,
        check: ApprovalCheck | None,
        test_scope: str | None = None,
    ) -> dict[str, Any]:
        approval = self.approval
        if not self.interactive or approval is None:
            return {
                "decision": "blocked",
                "reason": "Approval required; no interactive TTY or matching grant",
            }
        if check is not None:
            await check()
        answer = await approval(
            {
                **deepcopy(invocation),
                "one_shot": one_shot,
                **({"offer_test_scope": True} if test_scope else {}),
            }
        )
        if not one_shot and answer in {"session", "allow-session"}:
            self._session.add(test_scope or self._scope(invocation))
            return {
                "decision": "allow",
                "reason": "User allowed this session scope",
                **({"session_test_grant": True} if test_scope else {}),
            }
        if answer in {"once", "allow", "allow-once"}:
            return {"decision": "allow", "reason": "User allowed this invocation"}
        return {"decision": "deny", "reason": "User denied this invocation"}
