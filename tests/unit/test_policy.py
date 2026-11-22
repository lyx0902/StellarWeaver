from __future__ import annotations

import json
from pathlib import Path

import pytest

from stellarweaver.domain import JSONDict, ToolCall
from stellarweaver.evals.swebench_pilot import register_fixed_tools
from stellarweaver.policy import PolicyEngine
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.web_search import register_search


@pytest.mark.asyncio
async def test_no_tty_approval_is_blocked_and_never_prompts(tmp_path: Path) -> None:
    async def must_not_prompt(invocation: dict[str, object]) -> str:
        raise AssertionError("Non-interactive approval must not prompt")

    tools = ToolRegistry(
        tmp_path,
        PolicyEngine("ask", workspace=tmp_path, interactive=False, approval=must_not_prompt),
    )
    result = await tools.dispatch(ToolCall("c", "create_file", '{"path":"a.txt","content":"bad"}'))
    assert result.status == "blocked"
    assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_read_only_denies_even_direct_dispatch(tmp_path: Path) -> None:
    tools = ToolRegistry(tmp_path, PolicyEngine("read-only", workspace=tmp_path))
    assert "run_command" not in [schema["function"]["name"] for schema in tools.schemas()]
    result = await tools.dispatch(ToolCall("c", "create_file", '{"path":"a.txt","content":"bad"}'))
    assert result.status == "denied"
    assert not (tmp_path / "a.txt").exists()


async def test_auto_grants_edits_and_fixed_sandbox_commands_only(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    approvals: list[JSONDict] = []

    async def sandbox(argv: list[str], cwd: Path, **_: object) -> JSONDict:
        assert cwd == tmp_path
        commands.append(argv)
        return {"status": "ok", "data": {"exit_code": 0}}

    async def approve(invocation: JSONDict) -> str:
        approvals.append(invocation)
        return "deny"

    fixed = ("python", "-m", "unittest", "-q")
    policy = PolicyEngine("auto", workspace=tmp_path, interactive=True, approval=approve)
    tools = ToolRegistry(tmp_path, policy, command_runner=sandbox, sandbox_commands=(fixed,))
    assert (
        await tools.dispatch(ToolCall("c", "create_file", '{"path":"a.txt","content":"new"}'))
    ).status == "ok"
    assert (
        await tools.dispatch(ToolCall("fixed", "run_command", json.dumps({"argv": fixed})))
    ).status == "ok"
    assert approvals == [] and commands == [list(fixed)]
    rejected = await tools.dispatch(ToolCall("unlisted", "run_command", '{"argv":["python","-V"]}'))
    assert rejected.status == "denied" and commands == [list(fixed)]
    host = ToolRegistry(tmp_path, policy)
    rejected = await host.dispatch(ToolCall("host", "run_command", '{"argv":["python","-V"]}'))
    assert rejected.status == "denied"
    assert approvals[-1]["one_shot"] is True and approvals[-1]["security"]["level"] == "L2"


async def test_benchmark_fixed_tool_binds_actual_executor_and_rejects_egress(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    events: list[tuple[str, JSONDict]] = []
    fixed = ("python", "-m", "unittest", "-q")

    async def sandbox(argv: list[str], _: Path, **_options: object) -> JSONDict:
        calls.append(argv)
        return {"status": "ok", "data": {"exit_code": 0}}

    async def unexpected(_: str):
        pytest.fail("Benchmark must not forward a web query")

    grants = tmp_path / "policy.json"
    grants.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path),
                "grants": [
                    {"tool": "run_command", "argv": fixed, "cwd": "."},
                ],
            }
        ),
        encoding="utf-8",
    )
    tools = ToolRegistry(
        tmp_path,
        PolicyEngine("benchmark", workspace=tmp_path, policy_file=grants),
        journal=lambda kind, data: events.append((kind, data)),
    )
    command = ToolCall("host", "run_command", json.dumps({"argv": fixed}))
    assert (await tools.dispatch(command)).status == "denied"
    register_fixed_tools(tools, sandbox, {"run_visible_tests": ("Fixed checks", fixed)})
    register_search(tools, unexpected)
    names = {entry["function"]["name"] for entry in tools.schemas()}
    assert "run_visible_tests" in names and not names.intersection({"ds_search", "run_command"})
    assert (await tools.dispatch(ToolCall("fixed", "run_visible_tests", "{}"))).status == "ok"
    assert (
        await tools.dispatch(ToolCall("s", "ds_search", '{"query":"Python docs"}'))
    ).status == "denied"
    assert calls == [list(fixed)]
    assert any(
        data["tool"] == "run_visible_tests"
        and data["executor"] == "sandbox"
        and data["level"] == "L1"
        for kind, data in events
        if kind == "security.assessed"
    )


@pytest.mark.asyncio
async def test_policy_file_is_frozen_and_command_argv_is_exact(tmp_path: Path) -> None:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path),
                "grants": [
                    {"tool": "run_command", "argv": ["python", "-m", "pytest", "-q"], "cwd": "."},
                    {"tool": "create_file", "paths": ["allowed.txt"]},
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = PolicyEngine("ask", workspace=tmp_path, policy_file=policy_file)
    policy_file.write_text("{}", encoding="utf-8")
    invocation = {
        "tool": "run_command",
        "workspace": str(tmp_path),
        "argv": ["python", "-m", "pytest", "-q"],
        "cwd": ".",
    }
    assert (await policy.decide(invocation))["decision"] == "allow"
    assert (await policy.decide({**invocation, "argv": ["python", "-c", "dangerous()"]}))[
        "decision"
    ] == "blocked"
    create = {"tool": "create_file", "workspace": str(tmp_path), "path": "allowed.txt"}
    assert (await policy.decide(create))["decision"] == "allow"
    assert (await policy.decide({**create, "path": "other.txt"}))["decision"] == "blocked"


@pytest.mark.asyncio
async def test_session_grant_is_limited_to_same_path(tmp_path: Path) -> None:
    prompts: list[dict[str, object]] = []

    async def approve(invocation: dict[str, object]) -> str:
        prompts.append(invocation)
        return "session"

    policy = PolicyEngine("ask", workspace=tmp_path, interactive=True, approval=approve)
    invocation = {"tool": "edit_file", "workspace": str(tmp_path), "path": "one.txt"}
    assert (await policy.decide(invocation))["decision"] == "allow"
    assert (await policy.decide(invocation))["decision"] == "allow"
    assert len(prompts) == 1
    assert (await policy.decide({**invocation, "path": "two.txt"}))["decision"] == "allow"
    assert len(prompts) == 2


def test_policy_file_rejects_unbounded_or_different_workspace(tmp_path: Path) -> None:
    target = tmp_path / "policy.json"
    target.write_text(
        json.dumps(
            {"workspace": str(tmp_path), "grants": [{"tool": "run_command", "argv": ["python"]}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exact argv and cwd"):
        PolicyEngine(workspace=tmp_path, policy_file=target)
    target.write_text(json.dumps({"workspace": ".", "grants": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="absolute workspace"):
        PolicyEngine(workspace=tmp_path, policy_file=target)
