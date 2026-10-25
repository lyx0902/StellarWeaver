import asyncio
import hashlib
import io
import json
import signal
import sqlite3
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from stellarweaver.cli import execute_chat
from stellarweaver.domain import AssistantReply, ModelRequest, RunOutcome
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.ui.chat import ChatLoop
from stellarweaver.ui.console import TerminalUI


def quiet_ui() -> tuple[TerminalUI, io.StringIO]:
    output = io.StringIO()
    ui = TerminalUI()
    ui.console = Console(file=output, force_terminal=False, width=200)
    return ui, output


async def test_chat_keeps_one_conversation_and_handles_minimal_slash_commands() -> None:
    inputs = iter(["/help", "/status", "/unknown", "first task", "second task", "/exit"])
    calls: list[tuple[str, bool]] = []

    async def reader(prompt: str) -> str:
        return next(inputs)

    async def run(task: str, continued: bool) -> RunOutcome:
        calls.append((task, continued))
        return RunOutcome("completed", "unknown", "not_run", final_text=task)

    ui, output = quiet_ui()
    loop = ChatLoop(reader, run, ui, lambda: "session status", install_signal_handler=False)
    assert await loop.run() == 0
    assert calls == [("first task", False), ("second task", True)]
    assert "session status" in output.getvalue()
    assert "Unknown command" in output.getvalue()
    assert "Budgets reset for each submitted task" in output.getvalue()


async def test_idle_first_interrupt_clears_input_second_exits() -> None:
    async def reader(prompt: str) -> str:
        raise KeyboardInterrupt

    async def run(task: str, continued: bool) -> RunOutcome:
        raise AssertionError("No task should start while clearing input")

    ui, output = quiet_ui()
    loop = ChatLoop(reader, run, ui, lambda: "", install_signal_handler=False)
    assert await loop.run() == 130
    assert "Input cleared" in output.getvalue()


async def test_eof_exits_without_a_task() -> None:
    async def reader(prompt: str) -> str:
        raise EOFError

    async def run(task: str, continued: bool) -> RunOutcome:
        raise AssertionError("No task should start on EOF")

    ui, _ = quiet_ui()
    assert await ChatLoop(reader, run, ui, lambda: "").run() == 0


async def test_interrupt_at_approval_cancels_instead_of_denying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    class InterruptedPrompt:
        async def prompt_async(self, prompt: str, *, handle_sigint: bool) -> str:
            assert handle_sigint is False, "Active chat retains its cleanup-aware SIGINT handler"
            raise KeyboardInterrupt

    monkeypatch.setattr("stellarweaver.ui.console.PromptSession", InterruptedPrompt)
    ui, _ = quiet_ui()
    with pytest.raises(asyncio.CancelledError):
        await ui.approve({"workspace": ".", "tool": "create_file", "path": "test.py"})


async def test_cancelled_agent_request_is_cleaned_before_next_prompt(tmp_path: Path) -> None:
    started = asyncio.Event()
    cleaned = asyncio.Event()
    requests = 0

    async def reply(request: ModelRequest) -> AssistantReply:
        nonlocal requests
        requests += 1
        if requests == 1:
            started.set()
            try:
                await asyncio.Future[None]()
            finally:
                # Represents provider request/stream closing before another prompt.
                await asyncio.sleep(0)
                cleaned.set()
        assert cleaned.is_set()
        assert [message.content for message in request.messages if message.role == "user"] == [
            "first task",
            "follow-up task",
        ]
        return AssistantReply("follow-up complete")

    model = FakeModel(callback=reply)
    ui, _ = quiet_ui()
    with SessionStore(tmp_path, data_dir=tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        agent = Agent(model, registry, store=store, on_event=ui.event)
        prompt_index = 0
        outcomes: list[RunOutcome] = []

        async def reader(prompt: str) -> str:
            nonlocal prompt_index
            prompt_index += 1
            if prompt_index == 1:
                return "first task"
            assert cleaned.is_set(), "Input reopened before cancellation cleanup"
            return "follow-up task" if prompt_index == 2 else "/exit"

        async def run(task: str, continued: bool) -> RunOutcome:
            outcome = await agent.continue_run(task) if continued else await agent.run(task)
            outcomes.append(outcome)
            return outcome

        loop = ChatLoop(reader, run, ui, lambda: "", install_signal_handler=False)
        chat_task = asyncio.create_task(loop.run())
        await asyncio.wait_for(started.wait(), 2)
        loop.interrupt()
        assert await asyncio.wait_for(chat_task, 2) == 0
        assert [outcome.run_status for outcome in outcomes] == ["cancelled", "completed"]
        assert outcomes[0].session_id == outcomes[1].session_id
        assert outcomes[0].run_id != outcomes[1].run_id


async def test_second_interrupt_waits_for_cleanup_before_exit() -> None:
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()

    async def reader(prompt: str) -> str:
        assert not cleaned.is_set(), "Second interrupt should exit before prompting again"
        return "blocking task"

    async def run(task: str, continued: bool) -> RunOutcome:
        started.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()
            return RunOutcome("cancelled", "unknown", "not_run")
        raise AssertionError("Blocking task must be cancelled")

    ui, _ = quiet_ui()
    loop = ChatLoop(reader, run, ui, lambda: "", install_signal_handler=False)
    chat_task = asyncio.create_task(loop.run())
    await asyncio.wait_for(started.wait(), 2)
    loop.interrupt()
    await asyncio.wait_for(cleanup_started.wait(), 2)
    loop.interrupt()
    await asyncio.sleep(0)
    assert not chat_task.done()
    cleanup_release.set()
    assert await asyncio.wait_for(chat_task, 2) == 130
    assert cleaned.is_set()


async def test_active_signal_dispatch_cancels_and_restores_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    installed: list[object] = []
    previous = object()
    inputs = iter(["task", "/exit"])

    def install(signum: int, handler: object) -> object:
        assert signum == signal.SIGINT
        installed.append(handler)
        return previous

    monkeypatch.setattr(signal, "signal", install)

    async def reader(prompt: str) -> str:
        return next(inputs)

    async def run(task: str, continued: bool) -> RunOutcome:
        started.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            return RunOutcome("cancelled", "unknown", "not_run")
        raise AssertionError("Expected cancellation")

    ui, _ = quiet_ui()
    loop = ChatLoop(reader, run, ui, lambda: "")
    chat_task = asyncio.create_task(loop.run())
    await asyncio.wait_for(started.wait(), 2)
    handler = installed[0]
    assert callable(handler)
    handler(signal.SIGINT, None)
    assert await asyncio.wait_for(chat_task, 2) == 0
    assert installed[-1] is previous


async def test_signal_wakeup_reschedules_during_active_task_and_is_cancelled_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    ticked = asyncio.Event()
    inputs = iter(["waiting task", "/exit"])
    handles: list[asyncio.TimerHandle] = []

    async def reader(prompt: str) -> str:
        return next(inputs)

    async def run(task: str, continued: bool) -> RunOutcome:
        started.set()
        try:
            await asyncio.Future[None]()
        except asyncio.CancelledError:
            return RunOutcome("cancelled", "unknown", "not_run")
        raise AssertionError("Expected cancellation")

    ui, _ = quiet_ui()
    controller = ChatLoop(reader, run, ui, lambda: "")
    event_loop = asyncio.get_running_loop()
    original_call_later = event_loop.call_later

    def capture_call_later(delay, callback, *args, **kwargs):
        if callback == controller._signal_tick:
            assert delay == 0.1

            def deliver_tick() -> None:
                callback(*args)
                ticked.set()

            # Deliver the first tick promptly, then keep the next timer pending
            # so cleanup must explicitly cancel it instead of racing its expiry.
            handle = original_call_later(0.001 if not handles else 30, deliver_tick, **kwargs)
            handles.append(handle)
            return handle
        return original_call_later(delay, callback, *args, **kwargs)

    monkeypatch.setattr(event_loop, "call_later", capture_call_later)
    chat_task = asyncio.create_task(controller.run())
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(ticked.wait(), 2)
    assert len(handles) >= 2, "The wakeup must reschedule while the task remains active"
    controller.interrupt()
    assert await asyncio.wait_for(chat_task, 2) == 0
    assert handles[-1].cancelled()
    assert controller._signal_wakeup is None


async def test_chat_composition_reuses_provider_policy_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = iter(["first task", "second task", "/exit"])

    class Reader:
        async def prompt_async(self, prompt: str) -> str:
            return next(inputs)

    model = FakeModel([AssistantReply("first response"), AssistantReply("second response")])
    builds: list[object] = []

    def build(*args: object, **kwargs: object) -> FakeModel:
        builds.append(args)
        return model

    monkeypatch.setattr("stellarweaver.cli.PromptSession", Reader)
    monkeypatch.setattr("stellarweaver.cli.build_provider", build)
    code = await execute_chat(
        tmp_path, None, "fake", None, "read-only", None, tmp_path / "state", stream=False
    )
    assert code == 0
    assert len(builds) == 1
    assert len(model.requests) == 2
    assert [message.role for message in model.requests[1].messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    paths = list((tmp_path / "state").glob("*/session.sqlite3"))
    assert len(paths) == 1
    with sqlite3.connect(paths[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        messages = [
            json.loads(row[0])
            for row in connection.execute("SELECT body_json FROM messages ORDER BY position")
        ]
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
    ]


async def test_chat_cancel_before_first_agent_turn_can_submit_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = iter(["cancel before starting", "actual first task", "/exit"])

    class Reader:
        async def prompt_async(self, prompt: str) -> str:
            return next(inputs)

    class CancelBeforeStartChat(ChatLoop):
        interrupted_once = False

        def _signal_tick(self) -> None:
            # ChatLoop invokes this synchronously after create_task and before
            # awaiting it, reproducing Ctrl+C before the run coroutine starts.
            if not self.interrupted_once:
                self.interrupted_once = True
                self.interrupt()
            super()._signal_tick()

    model = FakeModel([AssistantReply("first actual task completed")])
    monkeypatch.setattr("stellarweaver.cli.PromptSession", Reader)
    monkeypatch.setattr("stellarweaver.cli.ChatLoop", CancelBeforeStartChat)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    code = await execute_chat(
        tmp_path, None, "fake", None, "read-only", None, tmp_path / "state", stream=False
    )
    assert code == 0
    assert len(model.requests) == 1
    assert [
        message.content for message in model.requests[0].messages if message.role == "user"
    ] == ["actual first task"]
    paths = list((tmp_path / "state").glob("*/session.sqlite3"))
    with sqlite3.connect(paths[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute("SELECT outcome_json FROM runs").fetchone()[0] is not None


async def test_all_m4_slash_commands_are_dispatched_without_submitting_a_model_task() -> None:
    inputs = iter(
        [
            "/help",
            "/model",
            "/model next-model",
            "/plan",
            "/plan build",
            "/diff",
            "/compact",
            "/undo",
            "/skill\tpython-tests",
            "/clear",
            "/exit",
        ]
    )
    commands: list[tuple[str, str]] = []

    async def reader(prompt: str) -> str:
        return next(inputs)

    async def run(task: str, continued: bool) -> RunOutcome:
        raise AssertionError("Slash commands must not become implicit model tasks")

    async def command(name: str, argument: str) -> str:
        commands.append((name, argument))
        return f"Executed {name} {argument}"

    ui, output = quiet_ui()
    controller = ChatLoop(
        reader, run, ui, lambda: "status", command=command, install_signal_handler=False
    )
    assert await controller.run() == 0
    assert commands == [
        ("/model", ""),
        ("/model", "next-model"),
        ("/plan", ""),
        ("/plan", "build"),
        ("/diff", ""),
        ("/compact", ""),
        ("/undo", ""),
        ("/skill", "python-tests"),
        ("/clear", ""),
    ]
    for name in ("/model", "/plan", "/diff", "/compact", "/undo", "/skill", "/clear"):
        assert name in output.getvalue()


async def test_slash_command_cancel_finishes_cleanup_before_returning_to_input() -> None:
    started, cleaned = asyncio.Event(), asyncio.Event()
    index = 0

    async def reader(prompt: str) -> str:
        nonlocal index
        index += 1
        if index == 1:
            return "/diff"
        assert cleaned.is_set()
        return "/exit"

    async def run(task: str, continued: bool) -> RunOutcome:
        raise AssertionError("No model task expected")

    async def command(name: str, argument: str) -> str:
        started.set()
        try:
            await asyncio.Future[None]()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
        return "unreachable"

    ui, _ = quiet_ui()
    controller = ChatLoop(
        reader, run, ui, lambda: "", command=command, install_signal_handler=False
    )
    task = asyncio.create_task(controller.run())
    await asyncio.wait_for(started.wait(), 2)
    controller.interrupt()
    assert await asyncio.wait_for(task, 2) == 0
    assert cleaned.is_set()


async def test_plan_build_does_not_escalate_initial_read_only_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stellarweaver.domain import ToolCall

    inputs = iter(["/plan", "/plan build", "attempt an edit", "/exit"])

    class Reader:
        async def prompt_async(self, prompt: str) -> str:
            return next(inputs)

    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(
                    ToolCall(
                        "denied-edit",
                        "create_file",
                        json.dumps({"path": "must-not-exist", "content": "x"}),
                    ),
                ),
                stop_reason="tool_calls",
            )
        ]
    )
    monkeypatch.setattr("stellarweaver.cli.PromptSession", Reader)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    assert (
        await execute_chat(tmp_path, None, "fake", None, "read-only", None, tmp_path / "state") == 0
    )
    assert not (tmp_path / "must-not-exist").exists()
    path = next((tmp_path / "state").glob("*/session.sqlite3"))
    snapshot = SessionStore.inspect(path)
    public = json.loads(snapshot.metadata["config_json"])
    assert public["policy"]["preset"] == "read-only"
    assert snapshot.tool_results[-1].status == "denied"
    assert len(model.requests) == 1


async def test_model_switch_revalidates_capabilities_before_replacing_live_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stellarweaver.config import AppConfig
    from stellarweaver.domain import ModelCapabilities

    inputs = iter(["/model too-small", "/model next-model", "a task", "/exit"])

    class Reader:
        async def prompt_async(self, prompt: str) -> str:
            return next(inputs)

    class TrackingProvider(FakeModel):
        def __init__(self, name: str) -> None:
            super().__init__([AssistantReply("new model response")])
            self.name = name
            self.closed = False

        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities(
                context_window_tokens=100 if self.name == "too-small" else 32768
            )

        async def aclose(self) -> None:
            self.closed = True

    models: list[TrackingProvider] = []

    def build(config: AppConfig, *args: object, **kwargs: object) -> TrackingProvider:
        model = TrackingProvider(config.model.model)
        models.append(model)
        return model

    monkeypatch.setattr("stellarweaver.cli.PromptSession", Reader)
    monkeypatch.setattr("stellarweaver.cli.OpenAICompatibleChatProvider", TrackingProvider)
    monkeypatch.setattr("stellarweaver.cli.build_provider", build)
    assert (
        await execute_chat(tmp_path, None, "deepseek", None, "read-only", None, tmp_path / "state")
        == 0
    )
    assert [model.name for model in models] == ["deepseek-flash", "too-small", "next-model"]
    assert all(model.closed for model in models)
    assert not models[0].requests and not models[1].requests
    assert len(models[2].requests) == 1
    path = next((tmp_path / "state").glob("*/session.sqlite3"))
    snapshot = SessionStore.inspect(path)
    assert json.loads(snapshot.metadata["config_json"])["model"]["model"] == "next-model"


async def test_skill_compact_and_clear_use_real_journal_without_extra_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = tmp_path / ".stellarweaver" / "skills" / "python-tests" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    marker = "SKILL_BODY_ONLY_AFTER_EXPLICIT_LOAD"
    skill.write_text(
        "---\nname: python-tests\ndescription: Choose real Python tests\n---\n" + marker + "\n"
    )
    inputs = iter(
        [
            "/skill python-tests",
            "explain the loaded skill",
            "/compact",
            "/clear",
            "begin a new task",
            "/exit",
        ]
    )
    seen = 0

    class Reader:
        async def prompt_async(self, prompt: str) -> str:
            return next(inputs)

    def reply(request: ModelRequest) -> AssistantReply:
        nonlocal seen
        if seen == 0:
            assert any(
                message.role == "tool" and marker in message.content for message in request.messages
            )
            assert request.messages[-1].content == "explain the loaded skill"
        else:
            assert request.messages[-1].content == "begin a new task"
            assert all(marker not in message.content for message in request.messages)
            assert all(
                "explain the loaded skill" not in message.content for message in request.messages
            )
        seen += 1
        return AssistantReply("Observed the actual current context.")

    model = FakeModel(callback=reply)
    ui, output = quiet_ui()
    monkeypatch.setattr("stellarweaver.cli.PromptSession", Reader)
    monkeypatch.setattr("stellarweaver.cli.TerminalUI", lambda: ui)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    assert (
        await execute_chat(
            tmp_path,
            None,
            "fake",
            None,
            "read-only",
            None,
            tmp_path / "state",
            trust_workspace=True,
        )
        == 0
    )
    assert len(model.requests) == 2
    path = next((tmp_path / "state").glob("*/session.sqlite3"))
    snapshot = SessionStore.inspect(path)
    assert any(
        result.tool_name == "load_skill" and result.status == "ok"
        for result in snapshot.tool_results
    )
    assert any(event.type == "skill.loaded" for event in snapshot.events)
    assert any(event.type == "context.cleared" for event in snapshot.events)
    assert any(
        message.role == "tool" and marker in message.content for message in snapshot.messages
    )
    assert "history_preserved" in output.getvalue()


async def test_diff_and_undo_restore_only_the_real_builtin_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stellarweaver.domain import ToolCall

    await asyncio.to_thread(
        subprocess.run, ["git", "init", "--quiet", str(tmp_path)], capture_output=True, check=True
    )
    path = tmp_path / "example.py"
    path.write_bytes(b"value = 1\n")
    baseline = path.read_bytes()
    unrelated = tmp_path / "user-notes.txt"
    unrelated.write_bytes(b"existing user notes\n")
    args = {
        "path": "example.py",
        "old_text": "1",
        "new_text": "2",
        "expected_sha256": hashlib.sha256(baseline).hexdigest(),
    }
    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(ToolCall("read", "read_file", json.dumps({"path": "example.py"})),),
                stop_reason="tool_calls",
            ),
            AssistantReply(
                tool_calls=(ToolCall("edit", "edit_file", json.dumps(args)),),
                stop_reason="tool_calls",
            ),
            AssistantReply("The builtin edit was applied."),
        ]
    )
    inputs = iter(["make the edit", "/diff", "/undo", "/exit"])

    class Reader:
        async def prompt_async(self, prompt: str) -> str:
            return next(inputs)

    ui, output = quiet_ui()
    monkeypatch.setattr("stellarweaver.cli.PromptSession", Reader)
    monkeypatch.setattr("stellarweaver.cli.TerminalUI", lambda: ui)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    assert (
        await execute_chat(tmp_path, None, "fake", None, "workspace-edit", None, tmp_path / "state")
        == 0
    )
    assert path.read_bytes() == baseline
    assert unrelated.read_bytes() == b"existing user notes\n"
    session = next((tmp_path / "state").glob("*/session.sqlite3"))
    snapshot = SessionStore.inspect(session)
    assert any(event.type == "changes.baseline" for event in snapshot.events)
    assert any(
        event.type == "changes.undo.completed" and event.payload["status"] == "ok"
        for event in snapshot.events
    )
    assert "baseline" in output.getvalue().lower()
    assert "example.py" in output.getvalue()
