from __future__ import annotations

import json
from pathlib import Path

import pytest

from stellarweaver.domain import ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.tools.registry import ToolRegistry


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
