from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from stellarweaver.tools.commands import (
    RunCommandArgs,
    controlled_environment,
    execute_command,
    prepare_run_command,
)
from stellarweaver.tools.registry import ToolContext
from stellarweaver.tools.workspace import ToolError, Workspace


def execution_config(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "default_timeout_seconds": 5,
            "max_timeout_seconds": 10,
            "max_output_bytes_per_stream": 1024,
            "max_artifact_bytes_per_stream": 4096,
            **kwargs,
        }
    )


async def test_true_exit_code_and_both_streams(tmp_path: Path) -> None:
    result = await execute_command(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err',file=sys.stderr); sys.exit(7)",
        ],
        tmp_path,
    )
    assert result["status"] == "error"
    assert result["data"]["exit_code"] == 7
    assert result["data"]["stdout"].splitlines() == ["out"]
    assert result["data"]["stderr"].splitlines() == ["err"]


async def test_error_word_does_not_override_success(tmp_path: Path) -> None:
    result = await execute_command([sys.executable, "-c", "print('error is just text')"], tmp_path)
    assert result["status"] == "ok"
    assert result["data"]["exit_code"] == 0


async def test_concurrent_large_streams_are_bounded_and_artifact_quota(tmp_path: Path) -> None:
    script = (
        "import os; os.write(1,b'HEAD'+b'x'*200000+b'TAIL'); os.write(2,b'ERR'+b'y'*200000+b'END')"
    )
    result = await execute_command(
        [sys.executable, "-c", script],
        tmp_path,
        config=execution_config(),
        artifacts_dir=tmp_path / "artifacts",
    )
    data = result["data"]
    assert result["status"] == "ok"
    assert data["stdout_truncated"] and data["stderr_truncated"]
    assert data["stdout"].startswith("HEAD") and data["stdout"].endswith("TAIL")
    assert data["stderr"].startswith("ERR") and data["stderr"].endswith("END")
    assert len(data["stdout"]) < 1200 and len(data["stderr"]) < 1200
    assert len(result["artifact_ids"]) == 2
    for artifact in data["artifacts"]:
        assert (await asyncio.to_thread(Path(artifact["path"]).stat)).st_size == 4096
        assert artifact["quota_exceeded"] is True
        assert artifact["total_bytes"] > 200000


async def test_small_output_does_not_leave_artifacts(tmp_path: Path) -> None:
    directory = tmp_path / "artifacts"
    result = await execute_command(
        [sys.executable, "-c", "print('small')"], tmp_path, artifacts_dir=directory
    )
    assert result["artifact_ids"] == ()
    assert list(directory.iterdir()) == []


async def test_credentials_and_process_injection_environment_are_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = [
        "STELLARWEAVER_API_KEY",
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OTHER_SECRET",
        "GIT_EXTERNAL_DIFF",
        "PYTHONSTARTUP",
    ]
    for name in names:
        monkeypatch.setenv(name, "secret-marker")
    result = await execute_command(
        [sys.executable, "-c", "import json,os; print(json.dumps(dict(os.environ)))"], tmp_path
    )
    environment = json.loads(result["data"]["stdout"])
    assert all(name not in environment for name in names)
    assert "PATH" in {name.upper() for name in environment}


async def test_argv_does_not_interpret_shell_syntax(tmp_path: Path) -> None:
    value = "$(echo unsafe) && echo unsafe > unwanted.txt"
    result = await execute_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", value], tmp_path
    )
    assert result["data"]["stdout"].strip() == value
    assert not (tmp_path / "unwanted.txt").exists()


async def test_cwd_is_explicit_and_does_not_mutate_parent(tmp_path: Path) -> None:
    child = tmp_path / "中文 with spaces"
    child.mkdir()
    original = Path.cwd()
    result = await execute_command([sys.executable, "-c", "import os; print(os.getcwd())"], child)
    assert Path(result["data"]["stdout"].strip()) == child
    assert Path.cwd() == original


async def test_timeout_is_structured_and_prompt(tmp_path: Path) -> None:
    started = time.monotonic()
    result = await execute_command(
        [sys.executable, "-c", "import time; print('started',flush=True); time.sleep(60)"],
        tmp_path,
        timeout_seconds=0.2,
    )
    assert time.monotonic() - started < 8
    assert result["status"] == "error"
    assert result["data"]["code"] == "COMMAND_TIMEOUT"
    assert result["data"]["timed_out"] is True
    assert result["data"]["cancelled"] is False


async def test_cancellation_is_structured_and_preserves_output(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    script = "from pathlib import Path; import time; print('ready',flush=True); Path('ready').write_text('yes'); time.sleep(60)"
    task = asyncio.create_task(execute_command([sys.executable, "-c", script], tmp_path))
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.02)
    assert ready.exists()
    task.cancel()
    result = await asyncio.wait_for(task, timeout=8)
    assert result["status"] == "cancelled"
    assert result["data"]["code"] == "COMMAND_CANCELLED"
    assert "ready" in result["data"]["stdout"]


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
    # Linux zombies are terminated even when reaping is delayed by PID 1.
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split()[2] == "Z")


async def test_timeout_cleans_attached_child_process(tmp_path: Path) -> None:
    script = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
    result = await execute_command([sys.executable, "-c", script], tmp_path, timeout_seconds=0.5)
    assert result["data"]["timed_out"]
    pid = int((tmp_path / "child.pid").read_text())
    for _ in range(50):
        if not process_alive(pid):
            break
        await asyncio.sleep(0.02)
    assert not process_alive(pid)


async def test_cancellation_cleans_attached_child_process(tmp_path: Path) -> None:
    script = "import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('child.pid').write_text(str(p.pid)); time.sleep(60)"
    task = asyncio.create_task(execute_command([sys.executable, "-c", script], tmp_path))
    for _ in range(100):
        if (tmp_path / "child.pid").exists():
            break
        await asyncio.sleep(0.02)
    pid = int((tmp_path / "child.pid").read_text())
    task.cancel()
    result = await asyncio.wait_for(task, timeout=8)
    assert result["status"] == "cancelled"
    for _ in range(50):
        if not process_alive(pid):
            break
        await asyncio.sleep(0.02)
    assert not process_alive(pid)


async def test_normal_parent_exit_cleans_remaining_group(tmp_path: Path) -> None:
    script = "import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path('child.pid').write_text(str(p.pid))"
    result = await execute_command([sys.executable, "-c", script], tmp_path, timeout_seconds=1)
    assert result["data"]["timed_out"] is False
    assert not process_alive(int((tmp_path / "child.pid").read_text()))
    if os.name == "nt":
        assert result["data"]["process_cleanup"] == "windows_job_object"
        assert result["data"]["process_management"]["cleanup_confirmed"]
        assert result["data"]["process_management"]["active_processes"] == 0


@pytest.mark.parametrize(
    "argv", [[], [""], ["python", "bad\x00argument"], ["python", "x" * 131073]]
)
def test_invalid_argv_rejected(argv: list[str]) -> None:
    with pytest.raises(ValidationError):
        RunCommandArgs(argv=argv)


async def test_timeout_above_config_limit_rejected_before_execution(tmp_path: Path) -> None:
    context = ToolContext(Workspace(tmp_path), tmp_path / "artifacts", execution_config())
    with pytest.raises(ToolError, match="cannot exceed"):
        await prepare_run_command(RunCommandArgs(argv=["python"], timeout_seconds=11.0), context)


async def test_workspace_cwd_escape_rejected(tmp_path: Path) -> None:
    context = ToolContext(Workspace(tmp_path), tmp_path / "artifacts")
    with pytest.raises(ToolError, match="relative path"):
        await prepare_run_command(RunCommandArgs(argv=["python"], cwd="../"), context)


def test_controlled_environment_never_copies_unknown_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_API_KEY", "not inherited")
    assert "CUSTOM_API_KEY" not in controlled_environment()
