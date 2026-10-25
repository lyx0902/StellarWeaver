"""Real setup phases share the task deadline and finalize only after cleanup."""

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stellarweaver.changes import ChangesManager
from stellarweaver.cli import app, execute_chat
from stellarweaver.config import LimitsConfig
from stellarweaver.domain import AssistantReply
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.budget import finish_cleanup
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry

runner = CliRunner()


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], capture_output=True, check=True)
    (root / "stellarweaver.toml").write_text("[limits]\nmax_wall_seconds=1\n", encoding="utf-8")
    return root


def emitted_outcome(output: str) -> dict:
    events = [json.loads(line) for line in output.splitlines()]
    durable = [event for event in events if event["type"] != "text.delta"]
    assert [event["seq"] for event in durable] == list(range(1, len(durable) + 1))
    return events[-1]["payload"]


def test_exec_baseline_timeout_persists_task_and_can_resume_without_repeating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = workspace(tmp_path)
    original = ChangesManager.capture_baseline

    async def slow(self: ChangesManager) -> dict:
        await asyncio.sleep(3)
        return await original(self)

    model = FakeModel()
    monkeypatch.setattr(ChangesManager, "capture_baseline", slow)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        [
            "exec",
            "preserve this task",
            "-w",
            str(root),
            "--provider",
            "fake",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 4, result.output
    outcome = emitted_outcome(result.output)
    assert outcome["run_status"] == "budget_exceeded"
    assert outcome["duration_ms"] >= 900
    assert not model.requests
    snapshot = SessionStore.inspect(outcome["state_path"])
    assert [message.content for message in snapshot.messages if message.role == "user"] == [
        "preserve this task"
    ]
    assert len(snapshot.runs) == 1

    monkeypatch.setattr(ChangesManager, "capture_baseline", original)
    # The recovery test concerns retaining the task, not whether three real Git
    # processes happen to fit within one second on this host.
    (root / "stellarweaver.toml").write_text("[limits]\nmax_wall_seconds=5\n", encoding="utf-8")
    resumed = runner.invoke(
        app,
        [
            "resume",
            outcome["session_id"],
            "--provider",
            "fake",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    snapshot = SessionStore.inspect(outcome["state_path"])
    assert len(model.requests) == 1
    assert len(snapshot.runs) == 2
    assert [message.content for message in snapshot.messages if message.role == "user"] == [
        "preserve this task"
    ]


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(
                kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
                and code.value == 259
            )
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_real_mcp_startup_is_interrupted_by_the_task_deadline_and_process_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = workspace(tmp_path)
    (root / "stellarweaver.toml").write_text("[limits]\nmax_wall_seconds=2\n", encoding="utf-8")
    server = tmp_path / "slow_server.py"
    pid_file = tmp_path / "server.pid"
    server.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(server), str(pid_file)]
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "workspace": str(root),
                "grants": [
                    {"tool": "mcp_start", "server_id": "slow", "argv": argv, "cwd": "."},
                ],
            }
        )
    )
    extensions = tmp_path / "extensions.json"
    extensions.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "server_id": "slow",
                        "command": sys.executable,
                        "argv": argv[1:],
                        "cwd": ".",
                        "startup_timeout_seconds": 20.0,
                    }
                ]
            }
        )
    )
    model = FakeModel()
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        [
            "exec",
            "read once startup is ready",
            "-w",
            str(root),
            "--provider",
            "fake",
            "--policy-file",
            str(policy),
            "--extensions",
            str(extensions),
            "--trust-workspace",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 4, result.output
    outcome = emitted_outcome(result.output)
    assert outcome["run_status"] == "budget_exceeded" and outcome["duration_ms"] >= 1900
    assert not model.requests
    assert pid_file.exists(), "The test must reach a real server process before timing out"
    assert not alive(int(pid_file.read_text()))
    snapshot = SessionStore.inspect(outcome["state_path"])
    assert any(
        message.role == "user" and message.content == "read once startup is ready"
        for message in snapshot.messages
    )
    assert len(snapshot.runs) == 1


def test_resume_reconciliation_time_is_included_and_new_task_is_retained(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = workspace(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        session_id = store.session_id
    original = SessionStore.reconcile

    def slow(self: SessionStore) -> dict:
        # SQLite stays on its owning thread. A synchronous phase is charged
        # after returning, even when it cannot be preempted by the event loop.
        time.sleep(1.05)
        return original(self)

    model = FakeModel()
    monkeypatch.setattr(SessionStore, "reconcile", slow)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        [
            "resume",
            session_id,
            "--prompt",
            "inspect recovered state",
            "--provider",
            "fake",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 4, result.output
    outcome = emitted_outcome(result.output)
    assert outcome["duration_ms"] >= 1000 and not model.requests
    snapshot = SessionStore.inspect(session_id, data_dir=tmp_path / "state")
    assert snapshot.messages[-1].content == "inspect recovered state"
    assert len(snapshot.runs) == 2


@pytest.mark.parametrize(
    "shutdown_delay,expected", [(0.08, "completed"), (1.05, "budget_exceeded")]
)
async def test_outcome_and_duration_are_saved_only_after_task_resources_close(
    tmp_path: Path,
    shutdown_delay: float,
    expected: str,
) -> None:
    closed = False
    with SessionStore(tmp_path, tmp_path / "state") as store:

        @asynccontextmanager
        async def resources():
            nonlocal closed
            try:
                yield
            finally:
                assert (
                    store.connection.execute(
                        "SELECT outcome_json FROM runs WHERE run_id=?", (store.run_id,)
                    ).fetchone()[0]
                    is None
                )
                await asyncio.sleep(shutdown_delay)
                closed = True
                store.append_event("test.resources_closed", {})

        model = FakeModel([AssistantReply("done")])
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(
            model,
            registry,
            store=store,
            limits=LimitsConfig(max_wall_seconds=1),
            preflight=resources,
        ).run("a bounded task")
        assert outcome.run_status == expected and closed
        assert outcome.duration_ms >= int(shutdown_delay * 1000)
        assert store.load_events()[-1].type == "run." + expected
        assert store.load_events()[-2].type == "test.resources_closed"


async def test_baseline_and_extension_entry_share_one_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ChangesManager.capture_baseline
    closed = False

    async def slow_baseline(self: ChangesManager) -> dict:
        await asyncio.sleep(0.6)
        return await original(self)

    @asynccontextmanager
    async def slow_entry():
        nonlocal closed
        try:
            await asyncio.sleep(0.6)
            yield
        finally:
            closed = True

    monkeypatch.setattr(ChangesManager, "capture_baseline", slow_baseline)
    model = FakeModel()
    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(
            model,
            registry,
            store=store,
            limits=LimitsConfig(max_wall_seconds=1),
            preflight=slow_entry,
        ).run("share all setup time")
        assert outcome.run_status == "budget_exceeded" and closed
        assert not model.requests and outcome.duration_ms >= 900


async def test_chat_user_thinking_does_not_consume_the_next_task_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "stellarweaver.toml").write_text("[limits]\nmax_wall_seconds=1\n")
    inputs = iter(["first", "second", "/exit"])

    class Reader:
        async def prompt_async(self, prompt: str) -> str:
            text = next(inputs)
            if text != "/exit":
                await asyncio.sleep(1.05)
            return text

    model = FakeModel()
    monkeypatch.setattr("stellarweaver.cli.PromptSession", Reader)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    assert (
        await execute_chat(tmp_path, None, "fake", None, "read-only", None, tmp_path / "state") == 0
    )
    snapshot = SessionStore.inspect(next((tmp_path / "state").glob("*/session.sqlite3")))
    outcomes = [json.loads(run["outcome_json"]) for run in snapshot.runs]
    assert all(outcome["run_status"] == "completed" for outcome in outcomes)
    assert all(outcome["duration_ms"] < 1000 for outcome in outcomes)
    assert len(model.requests) == 2


async def test_chat_setup_has_its_own_structured_timeout_before_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "stellarweaver.toml").write_text("[limits]\nmax_wall_seconds=1\n")

    async def slow(self: ChangesManager) -> dict:
        await asyncio.sleep(3)
        raise AssertionError("Deadline must interrupt setup")

    def no_prompt() -> None:
        raise AssertionError("A failed session setup must not open input")

    model = FakeModel()
    monkeypatch.setattr(ChangesManager, "capture_baseline", slow)
    monkeypatch.setattr("stellarweaver.cli.PromptSession", no_prompt)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    assert (
        await execute_chat(tmp_path, None, "fake", None, "read-only", None, tmp_path / "state") == 4
    )
    snapshot = SessionStore.inspect(next((tmp_path / "state").glob("*/session.sqlite3")))
    assert len(snapshot.runs) == 1 and not model.requests
    outcome = json.loads(snapshot.runs[0]["outcome_json"])
    assert outcome["run_status"] == "budget_exceeded" and outcome["duration_ms"] >= 900


async def test_repeated_cancel_during_cleanup_waits_and_preserves_cancelled_status(
    tmp_path: Path,
) -> None:
    closing = asyncio.Event()
    closed = False

    @asynccontextmanager
    async def resources():
        nonlocal closed
        try:
            yield
        finally:
            closing.set()
            await asyncio.sleep(0.08)
            closed = True

    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        agent = Agent(FakeModel(), registry, store=store, preflight=resources)
        task = asyncio.create_task(agent.run("cancel while resources close"))
        await asyncio.wait_for(closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        outcome = await asyncio.wait_for(task, 2)
        assert outcome.run_status == "cancelled" and closed
        assert store.load_events()[-1].type == "run.cancelled"
        assert not any(event.type == "run.completed" for event in store.load_events())


async def test_preflight_factory_failure_restores_agent_and_registry_hooks(tmp_path: Path) -> None:
    def broken_factory():
        raise ValueError("Invalid explicit setup configuration")

    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        agent = Agent(FakeModel(), registry, store=store, preflight=broken_factory)
        with pytest.raises(ValueError, match="Invalid explicit setup"):
            await agent.run("preserve the task before factory creation")
        assert not agent._running
        assert (
            registry.journal is None
            and registry.before_execute is None
            and registry.after_authorize is None
        )
        assert store.load_messages()[-1].content == "preserve the task before factory creation"


async def test_cleanup_that_swallows_cancellation_still_reports_the_missed_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = False

    @asynccontextmanager
    async def resources():
        nonlocal closed
        try:
            yield
        finally:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Matches a manager that catches cancellation while closing
                # one server, then finishes closing its remaining servers.
                await asyncio.sleep(0.15)
            closed = True

    async def short_grace(operation):
        return await finish_cleanup(operation, grace_seconds=0.01)

    monkeypatch.setattr("stellarweaver.runtime.loop.finish_cleanup", short_grace)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(FakeModel(), registry, store=store, preflight=resources).run(
            "finish owned cleanup"
        )
        assert closed, "Missed grace must not skip the remaining cooperative cleanup"
        assert outcome.run_status == "failed"
        assert "resource state unconfirmed" in outcome.reason
        assert any(event.type == "run.cleanup_failed" for event in store.load_events())
        assert not any(event.type == "run.completed" for event in store.load_events())
