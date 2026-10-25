"""Offline means fake model decisions, real files, Git and Python subprocesses."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from stellarweaver.config import ExecutionConfig, LimitsConfig
from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def call(name: str, args: dict[str, object], identifier: str = "call") -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall(identifier, name, json.dumps(args)),), stop_reason="tool_calls"
    )


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "中文 repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    (root / "用户已有改动.txt").write_bytes(b"user baseline\r\n")
    return root


def grant(root: Path, tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "workspace": str(root.resolve()),
                "grants": [
                    {"tool": "edit_file", "paths": ["calc.py"]},
                    {
                        "tool": "run_command",
                        "argv": [sys.executable, "-m", "unittest", "-q"],
                        "cwd": ".",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("fail_first", [False, True])
async def test_real_git_read_edit_test_repair(tmp_path: Path, fail_first: bool) -> None:
    root = workspace(tmp_path)
    policy = PolicyEngine(workspace=root, policy_file=grant(root, tmp_path))
    commands = []
    saw_failure = False
    edits = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal saw_failure, edits
        observations = [json.loads(m.content) for m in request.messages if m.role == "tool"]
        if not observations:
            return call("read_file", {"path": "calc.py"})
        last = observations[-1]
        assert last["status"] == "ok" or last["tool_name"] == "run_command"
        if last["tool_name"] == "read_file":
            edits += 1
            old = "return a - b" if edits == 1 else "return a + b + 1"
            new = "return a + b + 1" if fail_first and edits == 1 else "return a + b"
            return call(
                "edit_file",
                {
                    "path": "calc.py",
                    "old_text": old,
                    "new_text": new,
                    "expected_sha256": last["data"]["sha256"],
                },
            )
        if last["tool_name"] == "edit_file":
            return call("run_command", {"argv": [sys.executable, "-m", "unittest", "-q"]})
        if last["tool_name"] == "run_command":
            commands.append(last["data"]["exit_code"])
            if last["data"]["exit_code"] != 0:
                saw_failure = True
                assert "FAIL" in last["data"]["stderr"]
                return call("read_file", {"path": "calc.py"})
            return AssistantReply(text="Observed tests passed after real file edits.")
        raise AssertionError(last)

    model = FakeModel(script)
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, ExecutionConfig(), artifacts_dir=store.artifacts_dir)
        outcome = await Agent(model, registry, store=store).run("修复加法并运行测试")
        assert outcome.run_status == "completed"
        assert outcome.verification_status == "passed"
        assert outcome.task_assessment == "unknown"  # model claims are not an independent oracle
        assert commands == ([1, 0] if fail_first else [0])
        assert saw_failure == fail_first
        assert (root / "用户已有改动.txt").read_bytes() == b"user baseline\r\n"
        assert "return a + b\n" in (root / "calc.py").read_text()
        connection = sqlite3.connect(store.path)
        messages = [
            json.loads(row[0])
            for row in connection.execute("SELECT body_json FROM messages ORDER BY position")
        ]
        calls = sum(len(m["tool_calls"]) for m in messages if m["role"] == "assistant")
        assert calls == sum(m["role"] == "tool" for m in messages)
        assert calls == connection.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0]
        assert not connection.execute(
            "SELECT COUNT(*) FROM tool_executions WHERE result_json IS NULL"
        ).fetchone()[0]
        events = [json.loads(row[0]) for row in connection.execute("SELECT event_json FROM events")]
        assert events[-1]["type"] == "run.completed"
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        assert any(e["type"] == "tool.started" for e in events)
        connection.close()


async def test_no_tty_blocks_entire_remaining_round(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(
                    ToolCall("a", "create_file", '{"path":"new.py","content":"bad"}'),
                    ToolCall("b", "read_file", '{"path":"calc.py"}'),
                ),
                stop_reason="tool_calls",
            )
        ]
    )
    with SessionStore(root, tmp_path / "state") as store:
        agent = Agent(
            model,
            ToolRegistry(root, PolicyEngine(workspace=root), artifacts_dir=store.artifacts_dir),
            store=store,
        )
        result = await agent.run("create")
        assert result.run_status == "blocked"
        assert not (root / "new.py").exists()
        assert len(model.requests) == 1
        assert len(agent.results) == 2
        assert all(r.status == "blocked" for r in agent.results)


async def test_budget_mid_round_completes_protocol(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(
                    ToolCall("a", "read_file", '{"path":"calc.py"}'),
                    ToolCall("b", "read_file", '{"path":"test_calc.py"}'),
                ),
                stop_reason="tool_calls",
            )
        ]
    )
    with SessionStore(root, tmp_path / "state") as store:
        agent = Agent(
            model,
            ToolRegistry(root, PolicyEngine(workspace=root)),
            store=store,
            limits=LimitsConfig(max_tool_calls=1),
        )
        result = await agent.run("read")
        assert result.run_status == "budget_exceeded"
        assert [r.status for r in agent.results] == ["ok", "blocked"]


async def test_truncated_model_never_executes(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(ToolCall("a", "create_file", '{"path":"new.py","content":"no"}'),),
                stop_reason="length",
            )
        ]
    )
    with SessionStore(root, tmp_path / "state") as store:
        agent = Agent(
            model, ToolRegistry(root, PolicyEngine("workspace-edit", workspace=root)), store=store
        )
        result = await agent.run("write")
        assert result.run_status == "failed"
        assert result.tool_calls == 0
        assert len(agent.results) == 1
        assert not (root / "new.py").exists()


async def test_schema_counts_in_input_budget(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    model = FakeModel()
    with SessionStore(root, tmp_path / "state") as store:
        result = await Agent(
            model,
            ToolRegistry(root, PolicyEngine(workspace=root)),
            store=store,
            limits=LimitsConfig(max_input_tokens_total=100),
        ).run("hello")
        assert result.run_status == "budget_exceeded"
        assert not model.requests


async def test_duplicate_ids_and_provider_failure_are_not_success(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    duplicate = AssistantReply(
        tool_calls=(
            ToolCall("same", "read_file", '{"path":"calc.py"}'),
            ToolCall("same", "read_file", '{"path":"calc.py"}'),
        )
    )
    for replies in ([duplicate], []):
        with SessionStore(root, tmp_path / "state") as store:
            result = await Agent(
                FakeModel(replies), ToolRegistry(root, PolicyEngine(workspace=root)), store=store
            ).run("read")
            assert result.run_status == "failed"


async def test_claim_without_tools_is_not_verification(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        result = await Agent(
            FakeModel([AssistantReply(text="All tests passed!")]),
            ToolRegistry(root, PolicyEngine(workspace=root)),
            store=store,
        ).run("test")
        assert result.run_status == "completed"
        assert result.verification_status == "not_run"
        assert result.task_assessment == "unknown"
