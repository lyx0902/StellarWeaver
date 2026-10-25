"""Real Windows subprocesses retain Job cleanup uncertainty through the Agent."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import execute_command
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.win32_job import Win32Job


@pytest.mark.skipif(sys.platform != "win32", reason="Real Windows Job Object failure injection")
@pytest.mark.parametrize("tool", ["search_text", "git_status", "git_diff"])
async def test_real_cleanup_query_failure_stops_agent_and_followup_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    if tool == "search_text" and shutil.which("rg") is None:
        pytest.skip("Real rg executable required")
    root = tmp_path / "workspace"
    root.mkdir()
    await asyncio.to_thread(
        subprocess.run, ["git", "init", "--quiet", str(root)], capture_output=True, check=True
    )
    (root / "sample.txt").write_text("needle\n", encoding="utf-8")
    injected = False
    executions: list[dict[str, Any]] = []

    async def track(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = await execute_command(*args, **kwargs)
        if injected:
            executions.append(result)
        return result

    def refuse_accounting(self: Win32Job) -> dict[str, int]:
        raise OSError("Injected Job accounting refusal")

    def reply(request: ModelRequest) -> AssistantReply:
        nonlocal injected
        assert not injected, "Unknown tool execution must stop before another model request"
        # Baseline discovery remains healthy; only the actual requested tool fails.
        injected = True
        monkeypatch.setattr(Win32Job, "accounting", refuse_accounting)
        arguments = {"pattern": "needle"} if tool == "search_text" else {}
        return AssistantReply(
            tool_calls=(
                ToolCall("uncertain", tool, json.dumps(arguments)),
                ToolCall("must-not-run", "read_file", '{"path":"sample.txt"}'),
            ),
            stop_reason="tool_calls",
        )

    monkeypatch.setattr("stellarweaver.tools.commands.execute_command", track)
    monkeypatch.setattr("stellarweaver.tools.git.execute_command", track)
    model = FakeModel(callback=reply)
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(
            root, PolicyEngine(workspace=root), artifacts_dir=store.artifacts_dir
        )
        agent = Agent(model, registry, store=store)
        outcome = await agent.run("Inspect the workspace with the requested tool.")
        assert outcome.run_status == "failed"
        assert "execution state unknown" in outcome.reason
        assert len(model.requests) == len(executions) == 1
        raw = executions[0]
        assert raw["status"] == "unknown"
        assert raw["data"]["code"] == "JOB_CLEANUP_UNCONFIRMED"
        # Host Git config may contain filter names, giving 0 instead of the
        # otherwise normal no-filter exit 1. Neither proves Job cleanup.
        assert raw["data"]["exit_code"] in ({0} if tool == "search_text" else {0, 1})
        assert not raw["data"]["process_management"]["cleanup_confirmed"]
        assert agent.results[0].status == "unknown"
        assert agent.results[0].data == raw["data"]
        assert agent.results[1].status == "blocked"
        assert len(store.load_results()) == 2
        assert not any(event.type == "run.completed" for event in store.load_events())
        if tool == "search_text":
            assert "needle" in agent.results[0].data["stdout"]
        else:
            # Unknown filter enumeration must never start git status/diff itself.
            assert "config" in raw["data"]["argv"]
