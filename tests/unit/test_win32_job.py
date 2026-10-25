"""Real Windows Job Objects: no user code before assignment, no live descendants."""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from stellarweaver.tools.commands import controlled_environment, execute_command
from stellarweaver.tools.win32_job import CREATE_SUSPENDED, Win32Job

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object backend")


def alive(pid: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    kernel.GetExitCodeProcess.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return False
        raise ctypes.WinError(error)
    try:
        exit_code = ctypes.c_ulong()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return exit_code.value == 259
    finally:
        kernel.CloseHandle(handle)


def parent_script(root: Path, *, exit_parent: bool) -> list[str]:
    child = (
        "import os,time; from pathlib import Path; "
        "Path('child.ready').write_text(str(os.getpid())); time.sleep(60)"
    )
    script = root / "detaching_parent.py"
    script.write_text(
        "import os,subprocess,sys,time\nfrom pathlib import Path\n"
        "Path('parent.pid').write_text(str(os.getpid()))\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child!r}], "
        "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "Path('child.launcher.pid').write_text(str(child.pid))\n"
        "deadline=time.monotonic()+5\n"
        "while not Path('child.ready').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        "assert Path('child.ready').exists(), 'child did not start'\n"
        "print('detached child is running',flush=True)\n"
        + ("sys.exit(0)\n" if exit_parent else "time.sleep(60)\n"),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def proof(result: dict) -> None:
    info = result["data"]["process_management"]
    assert result["data"]["process_cleanup"] == "windows_job_object"
    assert info["assigned_before_resume"]
    assert info["kill_on_close"] and not info["breakaway_allowed"]
    assert info["cleanup_confirmed"] and info["active_processes"] == 0
    assert info["job_handle_closed"] and info["errors"] == []


def assert_dead(root: Path) -> None:
    for name in ("parent.pid", "child.launcher.pid", "child.ready"):
        assert not alive(int((root / name).read_text())), name


async def test_parent_exit_cleans_actively_detached_child_with_closed_pipes(tmp_path: Path) -> None:
    result = await execute_command(parent_script(tmp_path, exit_parent=True), tmp_path)
    assert result["status"] == "ok"
    assert result["data"]["exit_code"] == 0
    assert "detached child is running" in result["data"]["stdout"]
    assert_dead(tmp_path)
    proof(result)


async def test_timeout_cleans_actively_detached_child(tmp_path: Path) -> None:
    result = await execute_command(
        parent_script(tmp_path, exit_parent=False), tmp_path, timeout_seconds=5
    )
    assert result["data"]["code"] == "COMMAND_TIMEOUT"
    assert result["data"]["timed_out"]
    assert_dead(tmp_path)
    proof(result)


async def test_double_cancel_cleans_detached_descendants_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stellarweaver.tools import commands

    cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event()
    cleanup = commands._cleanup_windows

    async def delayed_cleanup(process: asyncio.subprocess.Process, job: Win32Job) -> dict:
        cleanup_started.set()
        await cleanup_release.wait()
        return await cleanup(process, job)

    monkeypatch.setattr(commands, "_cleanup_windows", delayed_cleanup)
    task = asyncio.create_task(
        execute_command(parent_script(tmp_path, exit_parent=False), tmp_path)
    )
    for _ in range(500):
        if (tmp_path / "child.ready").exists():
            break
        await asyncio.sleep(0.01)
    assert (tmp_path / "child.ready").exists()
    assert task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 5)
    assert task.cancel()  # The second cancellation occurs while teardown is in flight.
    cleanup_release.set()
    result = await asyncio.wait_for(task, 8)
    assert result["status"] == "cancelled"
    assert result["data"]["code"] == "COMMAND_CANCELLED"
    assert_dead(tmp_path)
    proof(result)


async def test_breakaway_request_cannot_escape_owned_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = "import os,time; from pathlib import Path; Path('breakaway.ready').write_text(str(os.getpid())); time.sleep(60)"
    script = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "try:\n"
        f"    child=subprocess.Popen([sys.executable,'-c',{child!r}], creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB)\n"
        "except OSError as exc:\n"
        "    assert exc.winerror == 5, exc\n"
        "    print('breakaway denied')\n"
        "else:\n"
        "    Path('breakaway.launcher.pid').write_text(str(child.pid))\n"
        "    deadline=time.monotonic()+5\n"
        "    while not Path('breakaway.ready').exists() and time.monotonic()<deadline: time.sleep(.01)\n"
        "    assert Path('breakaway.ready').exists()\n"
        "    print('breakaway child started inside nested job')\n"
    )
    memberships: list[bool] = []
    terminate = Win32Job.terminate

    def verify_membership(self: Win32Job) -> None:
        for name in ("breakaway.ready", "breakaway.launcher.pid"):
            if (tmp_path / name).exists():
                memberships.append(self.contains(int((tmp_path / name).read_text())))
        terminate(self)

    monkeypatch.setattr(Win32Job, "terminate", verify_membership)
    result = await execute_command([sys.executable, "-c", script], tmp_path)
    assert result["status"] == "ok", result
    if "breakaway denied" not in result["data"]["stdout"]:
        assert memberships == [True, True]
        assert not alive(int((tmp_path / "breakaway.ready").read_text()))
        assert not alive(int((tmp_path / "breakaway.launcher.pid").read_text()))
    proof(result)


async def test_assignment_failure_never_resumes_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owned_pids: list[int] = []

    def fail(self: Win32Job, pid: int) -> None:
        owned_pids.append(pid)
        raise OSError("Simulated nested-job assignment refusal")

    monkeypatch.setattr(Win32Job, "attach_and_resume", fail)
    result = await execute_command(
        [sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('unsafe')"],
        tmp_path,
    )
    assert result["status"] == "error"
    assert result["data"]["code"] == "PROCESS_CONTAINMENT_FAILED"
    assert not result["data"]["process_management"]["process_resumed"]
    assert result["data"]["process_management"]["cleanup_confirmed"]
    assert not (tmp_path / "ran.txt").exists()
    assert owned_pids and not alive(owned_pids[0])


async def test_unconfirmed_cleanup_is_never_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Win32Job.accounting

    def failed_query(self: Win32Job) -> dict[str, int]:
        # The job is still terminated and closed; only proof acquisition fails.
        original(self)
        raise OSError("Simulated query refusal")

    monkeypatch.setattr(Win32Job, "accounting", failed_query)
    result = await execute_command([sys.executable, "-c", "print('complete')"], tmp_path)
    assert result["status"] == "unknown"
    assert result["data"]["code"] == "JOB_CLEANUP_UNCONFIRMED"
    assert not result["data"]["process_management"]["cleanup_confirmed"]
    assert result["data"]["process_management"]["job_handle_closed"]


async def test_kill_on_close_alone_terminates_detached_descendants(tmp_path: Path) -> None:
    job = Win32Job()
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *parent_script(tmp_path, exit_parent=False),
            cwd=tmp_path,
            env=controlled_environment(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        job.attach_and_resume(process.pid)
        for _ in range(500):
            if (tmp_path / "child.ready").exists():
                break
            await asyncio.sleep(0.01)
        assert (tmp_path / "child.ready").exists()
        assert job.accounting()["active_processes"] >= 2
        assert job.contains(int((tmp_path / "child.ready").read_text()))
        # No TerminateJobObject call: prove the handle flag independently.
        job.close()
        await asyncio.wait_for(process.wait(), 5)
        assert_dead(tmp_path)
    finally:
        if job.handle is not None:
            job.terminate()
            job.close()
        if process is not None and process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 5)


async def test_ambiguous_primary_thread_fails_closed_after_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(self: Win32Job, pid: int) -> int:
        assert self.contains(pid)
        raise OSError("Cannot uniquely identify suspended primary thread")

    monkeypatch.setattr(Win32Job, "_primary_thread", refuse)
    result = await execute_command(
        [sys.executable, "-c", "from pathlib import Path; Path('ran.txt').write_text('unsafe')"],
        tmp_path,
    )
    assert result["status"] == "error"
    assert result["data"]["code"] == "PROCESS_CONTAINMENT_FAILED"
    assert not result["data"]["process_management"]["process_resumed"]
    assert result["data"]["process_management"]["cleanup_confirmed"]
    assert not (tmp_path / "ran.txt").exists()


async def test_cancellation_during_suspended_spawn_never_runs_user_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, release = asyncio.Event(), asyncio.Event()
    pids: list[int] = []
    create = asyncio.create_subprocess_exec

    async def paused_create(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await create(*args, **kwargs)
        pids.append(process.pid)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(
        "stellarweaver.tools.commands.asyncio.create_subprocess_exec", paused_create
    )
    task = asyncio.create_task(
        execute_command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('ran.txt').write_text('unsafe')",
            ],
            tmp_path,
        )
    )
    await asyncio.wait_for(created.wait(), 5)
    task.cancel()
    release.set()
    result = await asyncio.wait_for(task, 8)
    assert result["status"] == "cancelled"
    assert not result["data"]["process_management"]["process_resumed"]
    assert result["data"]["process_management"]["cleanup_confirmed"]
    assert not (tmp_path / "ran.txt").exists()
    assert pids and not alive(pids[0])
