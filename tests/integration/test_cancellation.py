"""Integration D: cancel a real managed process tree, then continue the session."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from stellarweaver.config import LimitsConfig
from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def process_alive(pid: int) -> bool:
    if os.name == "nt":
        # Query the owned PID directly; tasklist can fail when its WMI service is unavailable.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
                return False
            raise ctypes.WinError(error)
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split()[2] == "Z")


async def wait_for_file(path: Path) -> None:
    for _ in range(200):
        if await asyncio.to_thread(path.is_file) and (await asyncio.to_thread(path.stat)).st_size:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Process did not become ready: {path.name}")


async def test_cancel_real_process_tree_records_round_and_continues(tmp_path: Path) -> None:
    root = tmp_path / "temporary git workspace"
    root.mkdir()
    await asyncio.to_thread(
        subprocess.run, ["git", "init", "-q", str(root)], check=True, capture_output=True
    )
    child_code = (
        "import os,time; from pathlib import Path; "
        "Path('child.ready').write_text(str(os.getpid())); time.sleep(60)"
    )
    (root / "long_command.py").write_text(
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "print('parent and child started', flush=True)\n"
        "Path('parent.ready').write_text(str(os.getpid()))\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    argv = [sys.executable, "long_command.py"]
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "workspace": str(root),
                "grants": [{"tool": "run_command", "argv": argv, "cwd": "."}],
            }
        ),
        encoding="utf-8",
    )
    continuation_requests: list[ModelRequest] = []

    def script(request: ModelRequest) -> AssistantReply:
        if len([message for message in request.messages if message.role == "user"]) == 1:
            return AssistantReply(
                tool_calls=(
                    ToolCall("long-command", "run_command", json.dumps({"argv": argv})),
                    ToolCall(
                        "must-not-start", "create_file", '{"path":"never.txt","content":"no"}'
                    ),
                ),
                stop_reason="tool_calls",
            )
        continuation_requests.append(request)
        return AssistantReply(text="The cancelled command is recorded; the session continues.")

    with SessionStore(root, tmp_path / "state") as store:
        agent = Agent(
            FakeModel(script),
            ToolRegistry(
                root,
                PolicyEngine(workspace=root, policy_file=policy_path),
                artifacts_dir=store.artifacts_dir,
            ),
            store=store,
            limits=LimitsConfig(max_model_calls=1),
        )
        running = asyncio.create_task(agent.run("Start the long command, then write a file."))
        parent_pid = child_pid = None
        try:
            await wait_for_file(root / "parent.ready")
            await wait_for_file(root / "child.ready")
            parent_pid = int((root / "parent.ready").read_text())
            child_pid = int((root / "child.ready").read_text())
            assert process_alive(parent_pid) and process_alive(child_pid)
            running.cancel()
            cancelled = await asyncio.wait_for(running, timeout=12)
            assert cancelled.run_status == "cancelled"
            for _ in range(50):
                if not process_alive(parent_pid) and not process_alive(child_pid):
                    break
                await asyncio.sleep(0.02)
            assert not process_alive(parent_pid)
            assert not process_alive(child_pid)
            assert not (root / "never.txt").exists()
            assert [result.call_id for result in agent.results] == [
                "long-command",
                "must-not-start",
            ]
            assert all(result.status == "cancelled" for result in agent.results)
            command = agent.results[0]
            assert command.data["cancelled"] is True
            assert "parent and child started" in command.data["stdout"]
            rows = store.connection.execute(
                "SELECT call_id,state,intent_json,result_json FROM tool_executions"
            ).fetchall()
            assert len(rows) == 2
            assert {row[0] for row in rows} == {"long-command", "must-not-start"}
            assert all(row[1] == "cancelled" and row[3] is not None for row in rows)
            assert next(row for row in rows if row[0] == "long-command")[2] is not None
            continued = await agent.continue_run("Continue without restarting the command.")
            assert continued.run_status == "completed"
            assert continued.session_id == cancelled.session_id
            assert continued.run_id != cancelled.run_id
            assert continued.model_calls == 1
            assert continued.tool_calls == 0
            assert len(continuation_requests) == 1
            history = continuation_requests[0].messages
            assert [message.tool_call_id for message in history if message.role == "tool"] == [
                "long-command",
                "must-not-start",
            ]
            assert store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
            assert (
                store.connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE outcome_json IS NULL"
                ).fetchone()[0]
                == 0
            )
        finally:
            if not running.done():
                running.cancel()
                await asyncio.wait_for(running, timeout=12)
            # A failed regression must not leave the test's process tree behind.
            if parent_pid is None and (root / "parent.ready").exists():
                parent_pid = int((root / "parent.ready").read_text())
            if child_pid is None and (root / "child.ready").exists():
                child_pid = int((root / "child.ready").read_text())
            for pid in (parent_pid, child_pid):
                if pid is not None and process_alive(pid):
                    if os.name == "nt":
                        await asyncio.to_thread(
                            subprocess.run,
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True,
                            check=False,
                        )
                    else:
                        os.kill(pid, 9)
