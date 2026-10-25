"""Official SDK local stdio initialization, pagination, calls, failure and shutdown."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import sys
from pathlib import Path

import pytest

from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.extensions import ExtensionManager
from stellarweaver.extensions.mcp import tool_name
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry

SERVER = Path(__file__).parents[2] / "examples" / "mcp_server.py"


def setup(
    tmp_path: Path,
    *,
    preset: str = "ask",
    grant_start: bool = True,
    confirmed: tuple[str, ...] = ("lookup_symbol", "unsupported"),
    slow_grant: bool = False,
) -> tuple[Path, Path, PolicyEngine]:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("builtin remains available")
    argv = [sys.executable, str(SERVER)]
    grants = (
        [{"tool": "mcp_start", "server_id": "local fixture", "argv": argv, "cwd": "."}]
        if grant_start
        else []
    )
    if slow_grant:
        grants.append(
            {
                "tool": tool_name("local fixture", "slow_lookup"),
                "server_id": "local fixture",
                "arguments": {"symbol": "add"},
            }
        )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"workspace": str(root.resolve()), "grants": grants}))
    config = tmp_path / "extensions.json"
    config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "server_id": "local fixture",
                        "command": sys.executable,
                        "argv": [str(SERVER)],
                        "cwd": ".",
                        "read_only_tools": list(confirmed),
                        "call_timeout_seconds": 0.1,
                        "allow_env": [],
                    }
                ]
            }
        )
    )
    return root, config, PolicyEngine(preset, workspace=root, policy_file=policy_path)  # type: ignore[arg-type]


async def test_sdk_real_stdio_pagination_discovery_call_and_close(tmp_path: Path) -> None:
    root, config, policy = setup(tmp_path)
    events = []
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(
            registry, store, config, trusted=True, on_event=events.append
        ) as manager:
            assert manager.catalog[0]["status"] == "connected", manager.catalog
            assert manager.catalog[0]["protocol_version"]
            assert {tool["original_name"] for tool in manager.catalog[0]["tools"]} >= {
                "lookup_symbol",
                "slow_lookup",
                "unsupported",
            }
            names = [schema["function"]["name"] for schema in registry.schemas()]
            assert all(len(name) <= 64 for name in names)
            result = await registry.dispatch(
                ToolCall("call", tool_name("local fixture", "lookup_symbol"), '{"symbol":"add"}')
            )
            assert result.status == "ok", result
            assert result.data["structured_content"]["matches"][0] == {
                "path": "examples/fixtures/sample.py",
                "line": 4,
                "text": "def add(left: int, right: int) -> int:",
            }
            invalid = await registry.dispatch(
                ToolCall("bad", tool_name("local fixture", "lookup_symbol"), '{"symbol":123}')
            )
            assert invalid.status == "error" and invalid.data["code"] == "INVALID_ARGUMENTS"
            unsupported = await registry.dispatch(
                ToolCall("image", tool_name("local fixture", "unsupported"), '{"symbol":"add"}')
            )
            assert unsupported.data["unsupported_content_types"] == ["image"]
            assert "Unsupported MCP content types" in unsupported.content
            connection = manager._connections[0]
        assert connection.closed
        assert not any(name.startswith("mcp__") for name in registry._tools)
        closed = [event for event in store.load_events() if event.type == "mcp.closed"]
        assert len(closed) == 1
        artifact = closed[0].payload["stderr_artifact"]
        assert "local fixture initialized" in await asyncio.to_thread(
            Path(artifact["path"]).read_text
        )
        assert len(events) == len(store.load_events())


@pytest.mark.parametrize("trusted", [False, True])
async def test_trust_does_not_replace_exact_startup_authorization(
    tmp_path: Path, trusted: bool
) -> None:
    root, config, policy = setup(tmp_path, grant_start=False)
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=trusted) as manager:
            assert not manager._connections
            assert manager.catalog[0]["status"] == ("blocked" if trusted else "untrusted")
            assert not any(name.startswith("mcp__") for name in registry._tools)
            read = await registry.dispatch(ToolCall("read", "read_file", '{"path":"note.txt"}'))
            assert read.status == "ok"


async def test_read_only_exposes_only_user_confirmed_remote_tools_not_annotations(
    tmp_path: Path,
) -> None:
    root, config, policy = setup(tmp_path, preset="read-only")
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True):
            names = {schema["function"]["name"] for schema in registry.schemas()}
            assert tool_name("local fixture", "lookup_symbol") in names
            assert tool_name("local fixture", "slow_lookup") not in names
            denied = await registry.dispatch(
                ToolCall("denied", tool_name("local fixture", "slow_lookup"), '{"symbol":"add"}')
            )
            assert denied.status == "denied"
            assert not registry.is_mutation(tool_name("local fixture", "lookup_symbol"))
            assert registry.is_mutation(tool_name("local fixture", "slow_lookup"))


async def test_timeout_marks_unconfirmed_remote_effect_unknown_and_never_retries(
    tmp_path: Path,
) -> None:
    root, config, policy = setup(tmp_path, slow_grant=True)
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True) as manager:
            model = FakeModel(
                [
                    AssistantReply(
                        tool_calls=(
                            ToolCall(
                                "slow",
                                tool_name("local fixture", "slow_lookup"),
                                '{"symbol":"add"}',
                            ),
                        ),
                        stop_reason="tool_calls",
                    )
                ]
            )
            result = await Agent(model, registry, store=store).run(
                "Ask the explicitly granted local tool once"
            )
            assert result.run_status == "failed"
            assert result.tool_calls == 1
            assert len(model.requests) == 1
            assert store.load_results()[0].status == "unknown"
            assert store.load_results()[0].data["code"] == "MCP_RESULT_UNKNOWN"
            assert manager._connections[0].failed


async def test_mcp_tool_is_dispatched_and_journaled_in_real_agent_round(tmp_path: Path) -> None:
    root, config, policy = setup(tmp_path)
    step = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal step
        if step == 0:
            assert any(
                "Extension catalog" in message.content
                for message in request.messages
                if message.role == "system"
            )
            step += 1
            return AssistantReply(
                tool_calls=(
                    ToolCall(
                        "lookup", tool_name("local fixture", "lookup_symbol"), '{"symbol":"add"}'
                    ),
                ),
                stop_reason="tool_calls",
            )
        observed = json.loads(
            next(message.content for message in request.messages if message.role == "tool")
        )
        assert observed["data"]["structured_content"]["matches"][0]["line"] == 4
        return AssistantReply("The fixed fixture contains add on line 4.")

    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True):
            outcome = await Agent(FakeModel(script), registry, store=store).run(
                "Locate add in the fixed fixture"
            )
            assert outcome.run_status == "completed", outcome.reason
            operations = store.connection.execute(
                "SELECT state,result_json FROM tool_executions"
            ).fetchall()
            assert len(operations) == 1 and operations[0]["state"] == "succeeded"


async def test_tools_list_change_notification_refreshes_next_registry_snapshot(
    tmp_path: Path,
) -> None:
    root, config, policy = setup(tmp_path, confirmed=("lookup_symbol", "refresh_tools"))
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True) as manager:
            assert tool_name("local fixture", "slow_lookup") in registry._tools
            called = await registry.dispatch(
                ToolCall("change", tool_name("local fixture", "refresh_tools"), '{"symbol":"add"}')
            )
            assert called.status == "ok"
            await manager.refresh()
            assert tool_name("local fixture", "slow_lookup") not in registry._tools
            assert tool_name("local fixture", "lookup_second") in registry._tools
            assert any(event.type == "mcp.tools_updated" for event in store.load_events())


async def test_parent_secret_environment_is_not_inherited_by_mcp_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STELLARWEAVER_PRIVATE_TEST_TOKEN", "not-sent")
    root, config, policy = setup(tmp_path, confirmed=("environment_present",))
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True):
            result = await registry.dispatch(
                ToolCall(
                    "env",
                    tool_name("local fixture", "environment_present"),
                    '{"symbol":"STELLARWEAVER_PRIVATE_TEST_TOKEN"}',
                )
            )
            assert result.status == "ok"
            assert result.data["structured_content"] == {"present": False}


async def test_cancelled_unconfirmed_mcp_call_is_unknown_and_session_closes(tmp_path: Path) -> None:
    root, config, policy = setup(tmp_path, slow_grant=True)
    data = json.loads(config.read_text())
    data["servers"][0]["call_timeout_seconds"] = 10.0
    config.write_text(json.dumps(data))
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True) as manager:
            task = asyncio.create_task(
                registry.dispatch(
                    ToolCall(
                        "cancel", tool_name("local fixture", "slow_lookup"), '{"symbol":"add"}'
                    )
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            result = await task
            assert result.status == "unknown"
            assert result.data["code"] == "MCP_RESULT_UNKNOWN"
            assert result.data["cancelled"]
            assert manager._connections[0].closed


async def test_server_start_failure_keeps_builtin_tools_available(tmp_path: Path) -> None:
    root, config, _ = setup(tmp_path)
    data = json.loads(config.read_text())
    data["servers"][0]["command"] = "stellarweaver-nonexistent-test-command"
    config.write_text(json.dumps(data))

    async def approve(invocation: dict[str, object]) -> str:
        return "once"

    policy = PolicyEngine(workspace=root, interactive=True, approval=approve)
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True) as manager:
            assert manager.catalog[0]["status"] == "failed"
            read = await registry.dispatch(ToolCall("read", "read_file", '{"path":"note.txt"}'))
            assert read.status == "ok"
            assert any(event.type == "mcp.failed" for event in store.load_events())


async def test_disconnect_returns_unknown_for_unconfirmed_remote_call_without_replay(
    tmp_path: Path,
) -> None:
    root, config, _ = setup(tmp_path)
    grant_file = tmp_path / "policy.json"
    data = json.loads(grant_file.read_text())
    data["grants"].append(
        {
            "tool": tool_name("local fixture", "disconnect"),
            "server_id": "local fixture",
            "arguments": {"symbol": "add"},
        }
    )
    grant_file.write_text(json.dumps(data))
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(
            root,
            PolicyEngine(workspace=root, policy_file=grant_file),
            artifacts_dir=store.artifacts_dir,
        )
        async with ExtensionManager(registry, store, config, trusted=True):
            result = await registry.dispatch(
                ToolCall("exit", tool_name("local fixture", "disconnect"), '{"symbol":"add"}')
            )
            assert result.status == "unknown"
            assert result.data["code"] == "MCP_RESULT_UNKNOWN"
            assert result.data["retryable"] is False


def process_alive(pid: int) -> bool:
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


async def test_stdio_shutdown_ends_real_process_and_stderr_is_bounded_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "server.pid"
    monkeypatch.setenv("STELLARWEAVER_FIXTURE_PID_FILE", str(pid_path))
    monkeypatch.setenv("STELLARWEAVER_FIXTURE_STDERR_BYTES", "50000")
    monkeypatch.setenv("STELLARWEAVER_FIXTURE_LOG_TOKEN", "fake-secret-for-redaction-test")
    root, config, policy = setup(tmp_path)
    data = json.loads(config.read_text())
    data["servers"][0]["allow_env"] = [
        "STELLARWEAVER_FIXTURE_PID_FILE",
        "STELLARWEAVER_FIXTURE_STDERR_BYTES",
        "STELLARWEAVER_FIXTURE_LOG_TOKEN",
    ]
    config.write_text(json.dumps(data))
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        async with ExtensionManager(registry, store, config, trusted=True):
            pid = int(pid_path.read_text())
            assert process_alive(pid)
        assert not process_alive(pid)
        closed = next(event for event in store.load_events() if event.type == "mcp.closed")
        artifact = closed.payload["stderr_artifact"]
        assert artifact["total_bytes"] > 50000
        assert artifact["byte_length"] <= 32768
        assert artifact["truncated"]
        stderr = await asyncio.to_thread(Path(artifact["path"]).read_text)
        assert "fake-secret-for-redaction-test" not in stderr
        assert "[REDACTED]" in stderr


async def test_cancelled_second_server_initialization_cleans_first_real_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_path = tmp_path / "server.pid"
    monkeypatch.setenv("STELLARWEAVER_FIXTURE_PID_FILE", str(pid_path))
    monkeypatch.setenv("STELLARWEAVER_FIXTURE_STARTUP_DELAY", "5")
    root, config, _ = setup(tmp_path)
    data = json.loads(config.read_text())
    first = data["servers"][0]
    first["allow_env"] = ["STELLARWEAVER_FIXTURE_PID_FILE"]
    second = {
        **first,
        "server_id": "second",
        "allow_env": ["STELLARWEAVER_FIXTURE_PID_FILE", "STELLARWEAVER_FIXTURE_STARTUP_DELAY"],
    }
    data["servers"].append(second)
    config.write_text(json.dumps(data))
    policy_path = tmp_path / "policy.json"
    permission = json.loads(policy_path.read_text())
    permission["grants"].append({**permission["grants"][0], "server_id": "second"})
    policy_path.write_text(json.dumps(permission))
    first_ready = asyncio.Event()
    first_pid = 0

    async def observe(event: object) -> None:
        nonlocal first_pid
        if event.type == "mcp.connected":
            first_pid = int(pid_path.read_text())
            first_ready.set()

    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(
            root,
            PolicyEngine(workspace=root, policy_file=policy_path),
            artifacts_dir=store.artifacts_dir,
        )
        manager = ExtensionManager(registry, store, config, trusted=True, on_event=observe)
        entering = asyncio.create_task(manager.__aenter__())
        await asyncio.wait_for(first_ready.wait(), 10)
        second_pid = first_pid
        async with asyncio.timeout(10):
            while second_pid == first_pid:
                await asyncio.sleep(0.01)
                second_pid = int(pid_path.read_text())
        assert process_alive(first_pid) and process_alive(second_pid)
        entering.cancel()
        with pytest.raises(asyncio.CancelledError):
            await entering
        assert not process_alive(first_pid)
        assert not process_alive(second_pid)
        assert not manager._connections
        assert not any(name.startswith("mcp__") for name in registry._tools)
        assert registry.extension_context is None


async def test_close_callback_failure_still_cleans_all_registered_servers(tmp_path: Path) -> None:
    root, config, policy = setup(tmp_path)

    async def fail_close(event: object) -> None:
        if event.type == "mcp.closed":
            raise RuntimeError("simulated display failure")

    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, policy, artifacts_dir=store.artifacts_dir)
        manager = ExtensionManager(registry, store, config, trusted=True, on_event=fail_close)
        await manager.__aenter__()
        connection = manager._connections[0]
        with pytest.raises(RuntimeError, match="display failure"):
            await manager.aclose()
        assert connection.closed
        assert not manager._connections
        assert not any(name.startswith("mcp__") for name in registry._tools)
