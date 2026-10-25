"""M3 context/instruction integration uses real Git, files, commands and SQLite."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from stellarweaver.config import ContextConfig, LimitsConfig
from stellarweaver.context import ContextManager, estimate_tokens
from stellarweaver.domain import AssistantReply, JSONDict, ModelCapabilities, ModelRequest, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def reply(name: str, args: JSONDict) -> AssistantReply:
    # Real providers may legitimately reuse a call ID in a different assistant turn.
    return AssistantReply(
        tool_calls=(ToolCall("reused-call", name, json.dumps(args)),), stop_reason="tool_calls"
    )


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "中文 workspace"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    (root / "calc.py").write_bytes(b"def add(a, b):\n    return a - b\n")
    (root / "large.py").write_bytes(b"# " + b"source detail " * 500 + b"\nvalue = 1\n")
    (root / "user-baseline.txt").write_bytes(b"unchanged user baseline\r\n")
    return root


def policy(root: Path, tmp_path: Path, argv: list[str] | None = None) -> PolicyEngine:
    grants: list[JSONDict] = [{"tool": "edit_file", "paths": ["calc.py"]}]
    if argv is not None:
        grants.append({"tool": "run_command", "argv": argv, "cwd": "."})
    path = tmp_path / "grants.json"
    path.write_text(json.dumps({"workspace": str(root.resolve()), "grants": grants}))
    return PolicyEngine("workspace-edit", workspace=root, policy_file=path)


def wide_limits() -> LimitsConfig:
    return LimitsConfig(
        max_model_calls=60,
        max_tool_calls=60,
        max_input_tokens_total=2_000_000,
        max_output_tokens_total=100_000,
    )


def assert_complete_rounds(request: ModelRequest) -> None:
    # Building a projection also validates the complete local call/result protocol.
    ContextManager().build(
        request.messages,
        request.tools,
        1,
        10_000_000,
        ContextConfig(reserve_tokens=0),
    )


async def test_real_agent_compacts_complete_rounds_and_preserves_full_journal_evidence(
    tmp_path: Path,
) -> None:
    root = workspace(tmp_path)
    argv = [sys.executable, "-c", "from calc import add; assert add(2, 3) == 5"]
    step = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal step
        assert_complete_rounds(request)
        current = step
        step += 1
        if current == 0:
            return reply("read_file", {"path": "calc.py"})
        if current == 1:
            observed = json.loads(
                next(item.content for item in request.messages if item.role == "tool")
            )
            return reply(
                "edit_file",
                {
                    "path": "calc.py",
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                    "expected_sha256": observed["data"]["sha256"],
                },
            )
        if current == 2:
            return reply("run_command", {"argv": argv})
        if current < 15:
            return reply("read_file", {"path": "large.py"})
        return AssistantReply("Real edit and command results recorded; source reviewed.")

    class PressureModel(FakeModel):
        def capabilities(self) -> ModelCapabilities:
            # This test deliberately retains its original pressure window;
            # raising the application default must not remove compaction coverage.
            return ModelCapabilities(streaming=True, context_window_tokens=32768)

    model = PressureModel(script)
    with SessionStore(root, tmp_path / "state") as store:
        agent = Agent(
            model,
            ToolRegistry(root, policy(root, tmp_path, argv), artifacts_dir=store.artifacts_dir),
            store=store,
            limits=wide_limits(),
            max_output_tokens=256,
            context_config=ContextConfig(reserve_tokens=256, recent_complete_rounds=2),
        )
        outcome = await agent.run(
            "Fix calc.py, verify add(2, 3), then inspect large.py repeatedly."
        )
        assert outcome.run_status == "completed", outcome.reason
        assert outcome.verification_status == "passed"
        assert outcome.tool_calls == 15
        assert (root / "calc.py").read_bytes() == b"def add(a, b):\n    return a + b\n"
        assert (root / "user-baseline.txt").read_bytes() == b"unchanged user baseline\r\n"
        raw = store.load_messages()
        assert len(raw) == 2 + 2 * outcome.tool_calls + 1
        for persisted, live in zip(raw, agent.messages, strict=True):
            assert (persisted.role, persisted.tool_calls, persisted.tool_call_id) == (
                live.role,
                live.tool_calls,
                live.tool_call_id,
            )
            if persisted.role == "tool":
                assert json.loads(persisted.content) == json.loads(live.content)
            else:
                assert persisted.content == live.content
        assert sum(item.role == "tool" for item in raw) == outcome.tool_calls
        original_large_results = [
            item for item in raw if item.role == "tool" and "source detail" in item.content
        ]
        assert len(original_large_results) == 12
        assert all(len(item.content) > 10000 for item in original_large_results)
        assert all("projection truncated" not in item.content for item in original_large_results)
        assert any(
            "projection truncated" in item.content
            for request in model.requests
            for item in request.messages
            if item.role == "tool"
        )
        events = store.load_events()
        compacted = [event for event in events if event.type == "context.compacted"]
        assert compacted
        assert all(
            event.payload["after_tokens_estimated"] < event.payload["before_tokens_estimated"]
            for event in compacted
        )
        checkpoint = store.read_checkpoint()
        assert checkpoint is not None
        summary = checkpoint["summary"]
        assert summary["source"] == "recorded_messages_and_tool_results"
        change = next(
            record for record in summary["changed_files"] if record["data"]["path"] == "calc.py"
        )
        assert (
            change["data"]["after_sha256"]
            == hashlib.sha256((root / "calc.py").read_bytes()).hexdigest()
        )
        command = next(
            record for record in summary["verified_commands"] if record["data"]["argv"] == argv
        )
        assert command["data"]["exit_code"] == 0
        assert command["status"] == "ok"
        assert checkpoint["through_event_seq"] <= events[-1].seq
        assert all(event.seq == index for index, event in enumerate(events, start=1))


async def test_new_subdirectory_instructions_are_seen_before_first_mutation(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    (root / "AGENTS.md").write_text("ROOT_RULE: preserve user baseline", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src/AGENTS.md").write_text(
        "SUBDIR_RULE: review this before creating src files", encoding="utf-8"
    )
    step = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal step
        text = "\n".join(item.content for item in request.messages if item.role == "system")
        assert "ROOT_RULE" in text
        if step == 0:
            assert "SUBDIR_RULE" not in text
            step += 1
            return reply("create_file", {"path": "src/new.txt", "content": "reviewed"})
        if step == 1:
            assert not (root / "src/new.txt").exists()
            assert "SUBDIR_RULE" in text
            result = json.loads(
                [item.content for item in request.messages if item.role == "tool"][-1]
            )
            assert result["data"]["code"] == "INSTRUCTIONS_UPDATED"
            step += 1
            return reply("create_file", {"path": "src/new.txt", "content": "reviewed"})
        assert (root / "src/new.txt").read_text() == "reviewed"
        return AssistantReply("Created after reviewing the applicable scope.")

    model = FakeModel(script)
    with SessionStore(root, tmp_path / "state") as store:
        agent = Agent(
            model,
            ToolRegistry(
                root,
                PolicyEngine("workspace-edit", workspace=root),
                artifacts_dir=store.artifacts_dir,
            ),
            store=store,
        )
        outcome = await agent.run("Create src/new.txt")
        assert outcome.run_status == "completed", outcome.reason
        assert [result.status for result in agent.results] == ["error", "ok"]
        assert len([event for event in store.load_events() if event.type == "tool.started"]) == 1
        assert len(model.requests) == 3


async def test_instruction_change_between_model_and_mutation_reloads_hash_event(
    tmp_path: Path,
) -> None:
    root = workspace(tmp_path)
    instruction = root / "AGENTS.md"
    instruction.write_text("FIRST_RULE", encoding="utf-8")
    initial_hash = hashlib.sha256(instruction.read_bytes()).hexdigest()
    step = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal step
        system = "\n".join(item.content for item in request.messages if item.role == "system")
        if step == 0:
            assert "FIRST_RULE" in system
            instruction.write_text("CHANGED_RULE: inspect revised constraints", encoding="utf-8")
            step += 1
            return reply("create_file", {"path": "new.txt", "content": "reviewed change"})
        if step == 1:
            assert not (root / "new.txt").exists()
            assert "CHANGED_RULE" in system
            assert "FIRST_RULE" not in system
            step += 1
            return reply("create_file", {"path": "new.txt", "content": "reviewed change"})
        return AssistantReply("Created after the changed instructions were reviewed.")

    with SessionStore(root, tmp_path / "state") as store:
        agent = Agent(
            FakeModel(script),
            ToolRegistry(
                root,
                PolicyEngine("workspace-edit", workspace=root),
                artifacts_dir=store.artifacts_dir,
            ),
            store=store,
        )
        outcome = await agent.run("Create new.txt")
        assert outcome.run_status == "completed", outcome.reason
        hashes = [
            source["sha256"]
            for event in store.load_events()
            if event.type == "instructions.loaded"
            for source in event.payload["sources"]
            if source["source_path"] == "AGENTS.md"
        ]
        assert hashes == [initial_hash, hashlib.sha256(instruction.read_bytes()).hexdigest()]
        assert agent.results[0].data["code"] == "INSTRUCTIONS_UPDATED"
        assert (root / "new.txt").read_text() == "reviewed change"


@pytest.mark.parametrize("fails_again", [False, True])
async def test_server_context_failure_gets_only_one_smaller_retry_without_replaying_tool(
    tmp_path: Path, fails_again: bool
) -> None:
    root = workspace(tmp_path)
    code = (
        "from pathlib import Path; p = Path('counter.txt'); "
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1'); "
        "print('x' * 6000)"
    )
    argv = [sys.executable, "-c", code]
    step = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal step
        current = step
        step += 1
        if current == 0:
            return reply("run_command", {"argv": argv})
        if current == 1 or fails_again:
            raise ProviderError(
                "Model context length exceeded", context_length_exceeded=True, status_code=400
            )
        assert (root / "counter.txt").read_text() == "1"
        assert_complete_rounds(request)
        return AssistantReply("Observed the command once after a smaller context retry.")

    model = FakeModel(script)
    with SessionStore(root, tmp_path / "state") as store:
        outcome = await Agent(
            model,
            ToolRegistry(root, policy(root, tmp_path, argv), artifacts_dir=store.artifacts_dir),
            store=store,
            limits=wide_limits(),
            max_output_tokens=256,
            context_config=ContextConfig(reserve_tokens=256, recent_complete_rounds=1),
        ).run("Run the counter command exactly once and inspect its output.")
        assert outcome.run_status == ("failed" if fails_again else "completed"), outcome.reason
        assert (root / "counter.txt").read_text() == "1"
        assert outcome.model_calls == len(model.requests) == 3
        assert outcome.tool_calls == 1
        events = store.load_events()
        retries = [event for event in events if event.type == "context.retry"]
        assert len(retries) == 1
        assert (
            retries[0].payload["after_tokens_estimated"]
            < retries[0].payload["before_tokens_estimated"]
        )
        assert len([event for event in events if event.type == "tool.started"]) == 1
        failed_size = estimate_tokens(
            {
                "messages": [asdict(item) for item in model.requests[1].messages],
                "tools": model.requests[1].tools,
            }
        )
        retry_size = estimate_tokens(
            {
                "messages": [asdict(item) for item in model.requests[2].messages],
                "tools": model.requests[2].tools,
            }
        )
        assert retry_size < failed_size
        assert sum(item.role == "tool" for item in store.load_messages()) == 1


async def test_fixed_tool_schema_budget_failure_makes_no_provider_call(tmp_path: Path) -> None:
    root = workspace(tmp_path)

    class TinyWindowModel(FakeModel):
        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities(context_window_tokens=1500)

    model = TinyWindowModel()
    with SessionStore(root, tmp_path / "state") as store:
        outcome = await Agent(
            model,
            ToolRegistry(root, PolicyEngine(workspace=root), artifacts_dir=store.artifacts_dir),
            store=store,
            max_output_tokens=128,
            context_config=ContextConfig(reserve_tokens=128),
        ).run("hello")
        assert outcome.run_status == "budget_exceeded"
        assert "tool schemas" in outcome.reason
        assert outcome.model_calls == 0
        assert not model.requests
        assert not any(event.type == "model.started" for event in store.load_events())
