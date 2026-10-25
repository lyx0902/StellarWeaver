"""Regression checks for runtime-controlled budgets and committed tool rounds."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from stellarweaver.config import ExecutionConfig, LimitsConfig
from stellarweaver.domain import AgentEvent, AssistantReply, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def tool_reply(name: str, args: dict[str, object], call_id: str = "call") -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall(call_id, name, json.dumps(args)),), stop_reason="tool_calls"
    )


async def test_model_call_budget_stops_without_a_third_request(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    model = FakeModel(callback=lambda _: tool_reply("read_file", {"path": "a.txt"}))
    with SessionStore(tmp_path, tmp_path / "state") as store:
        tools = ToolRegistry(
            tmp_path, PolicyEngine(workspace=tmp_path), artifacts_dir=store.artifacts_dir
        )
        agent = Agent(model, tools, limits=LimitsConfig(max_model_calls=2), store=store)
        outcome = await agent.run("read repeatedly")
        assert outcome.run_status == "budget_exceeded"
        assert outcome.reason == "max_model_calls"
        assert outcome.model_calls == len(model.requests) == 2
        assert len(agent.results) == 2


@pytest.mark.parametrize("budget", ["input", "output"])
async def test_reported_token_overrun_blocks_every_side_effect(tmp_path: Path, budget: str) -> None:
    usage = {
        "prompt_tokens": 100001 if budget == "input" else 100,
        "completion_tokens": 100001 if budget == "output" else 10,
    }
    reply = tool_reply("create_file", {"path": "forbidden.txt", "content": "must not write"})
    model = FakeModel(
        [AssistantReply(tool_calls=reply.tool_calls, stop_reason="tool_calls", usage=usage)]
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        tools = ToolRegistry(
            tmp_path,
            PolicyEngine("workspace-edit", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        agent = Agent(
            model,
            tools,
            limits=LimitsConfig(max_input_tokens_total=100000, max_output_tokens_total=100000),
            store=store,
        )
        outcome = await agent.run("write")
        assert outcome.run_status == "budget_exceeded"
        assert outcome.tool_calls == 0
        assert len(agent.results) == 1
        assert agent.results[0].status == "blocked"
        assert not (tmp_path / "forbidden.txt").exists()


async def test_three_identical_large_command_failures_stop_before_fourth(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "import sys; print('x' * 2000); sys.exit(1)"]
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path),
                "grants": [{"tool": "run_command", "argv": argv, "cwd": "."}],
            }
        ),
        encoding="utf-8",
    )
    model = FakeModel(callback=lambda _: tool_reply("run_command", {"argv": argv}))
    with SessionStore(tmp_path, tmp_path / "state") as store:
        tools = ToolRegistry(
            tmp_path,
            PolicyEngine(workspace=tmp_path, policy_file=policy_file),
            execution=ExecutionConfig(max_output_bytes_per_stream=1024),
            artifacts_dir=store.artifacts_dir,
        )
        agent = Agent(model, tools, limits=LimitsConfig(max_model_calls=5), store=store)
        outcome = await agent.run("run failing command")
        assert outcome.run_status == "failed"
        assert outcome.reason == "three identical tool failures"
        assert outcome.model_calls == 3
        assert len(agent.results) == 3
        assert all(result.data["exit_code"] == 1 for result in agent.results)
        assert all(result.data["stdout_truncated"] for result in agent.results)


@pytest.mark.parametrize("cancel_event", ["assistant.completed", "tool.completed", "tool.finished"])
async def test_cancel_during_event_delivery_completes_committed_tool_round(
    tmp_path: Path, cancel_event: str
) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    interrupted = False

    async def events(event: AgentEvent) -> None:
        nonlocal interrupted
        if event.type == cancel_event and not interrupted:
            interrupted = True
            raise asyncio.CancelledError

    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(
                    ToolCall("first", "read_file", '{"path":"a.txt"}'),
                    ToolCall("second", "create_file", '{"path":"never.txt","content":"bad"}'),
                ),
                stop_reason="tool_calls",
            )
        ]
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        tools = ToolRegistry(
            tmp_path,
            PolicyEngine("workspace-edit", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        agent = Agent(model, tools, store=store, on_event=events)
        outcome = await agent.run("read and create")
        assert outcome.run_status == "cancelled"
        assert not (tmp_path / "never.txt").exists()
        assert [result.call_id for result in agent.results] == ["first", "second"]
        assert agent.results[-1].status == "cancelled"
        rows = store.connection.execute(
            "SELECT call_id,result_json FROM tool_executions"
        ).fetchall()
        assert {row[0] for row in rows} == {"first", "second"}
        assert all(row[1] is not None for row in rows)
