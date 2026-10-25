"""Bounded argv execution on the host, with explicit process ownership.

Authorization belongs to the registry. This backend is deliberately not an OS
sandbox: an approved executable can access files outside its working directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
import uuid
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stellarweaver.tools.win32_job import CREATE_NEW_PROCESS_GROUP, CREATE_SUSPENDED, Win32Job
from stellarweaver.tools.workspace import ToolError

if TYPE_CHECKING:
    from stellarweaver.domain import JSONDict
    from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry


class RunCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = "."
    timeout_seconds: float | None = Field(default=None, gt=0)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if not value[0] or any("\x00" in item for item in value):
            raise ValueError("argv requires a nonempty executable and cannot contain NUL bytes")
        if sum(len(item) for item in value) > 131_072:
            raise ValueError("argv exceeds the 128 KiB argument limit")
        return value


def controlled_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Inherit only OS/runtime essentials, never arbitrary provider credentials."""
    names = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "VIRTUAL_ENV",
        "TERM",
        "TZ",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in names}
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
    if overrides:
        environment.update(overrides)
    return environment


class _Capture:
    """Keep a bounded head/tail and a separately bounded on-disk stream."""

    def __init__(self, limit: int, artifact_limit: int, directory: Path | None, stream: str):
        self.limit = max(2, limit)
        self.artifact_limit = max(0, artifact_limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self.saved = 0
        self.path = directory / f"{uuid.uuid4().hex}.{stream}.txt" if directory else None
        self._file = self.path.open("xb") if self.path else None

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self._file is not None:
            available = self.artifact_limit - self.saved
            if available > 0:
                retained = chunk[:available]
                self._file.write(retained)
                self.saved += len(retained)
        head_size = self.limit // 2
        take = min(max(0, head_size - len(self.head)), len(chunk))
        self.head.extend(chunk[:take])
        self.tail.extend(chunk[take:])
        tail_size = self.limit - head_size
        if len(self.tail) > tail_size:
            del self.tail[:-tail_size]

    async def read(self, reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(65_536):
            self.feed(chunk)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self.path is not None and self.total <= self.limit:
            self.path.unlink(missing_ok=True)

    def text(self) -> str:
        marker = b"\n[... OUTPUT_TRUNCATED: see artifact ...]\n" if self.total > self.limit else b""
        return (bytes(self.head) + marker + bytes(self.tail)).decode("utf-8", errors="replace")

    def artifact(self) -> JSONDict | None:
        if self.path is None or self.total <= self.limit:
            return None
        return {
            "artifact_id": self.path.name,
            "path": str(self.path),
            "total_bytes": self.total,
            "saved_bytes": self.saved,
            "quota_exceeded": self.total > self.saved,
        }


async def _wait_cleanup(awaitable: Awaitable[Any]) -> Any:
    """Finish cleanup even if a caller repeats cancellation during teardown."""
    task = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def _terminate_tree(process: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        raise RuntimeError("Windows commands require an owned Job Object")
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except TimeoutError:
        pass
    # Always kill surviving descendants, including when their parent has exited.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


async def _cleanup_windows(process: asyncio.subprocess.Process, job: Win32Job) -> JSONDict:
    """Terminate the whole job and confirm zero active processes before closing it."""
    errors: list[JSONDict] = []
    accounting: JSONDict = {}
    deadline = time.monotonic() + 5.0
    try:
        try:
            job.terminate()
        except OSError as exc:
            errors.append({"phase": "terminate", "winerror": getattr(exc, "winerror", None)})
        if not job.assigned and process.returncode is None:
            # A startup failure leaves only the never-resumed primary process.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        while time.monotonic() < deadline:
            try:
                accounting = job.accounting()
            except OSError as exc:
                errors.append({"phase": "accounting", "winerror": getattr(exc, "winerror", None)})
                break
            if accounting["active_processes"] == 0:
                break
            await asyncio.sleep(0.02)
        try:
            await asyncio.wait_for(
                _wait_parent_exit(process), max(0.01, deadline - time.monotonic())
            )
        except TimeoutError:
            errors.append({"phase": "parent_wait", "reason": "cleanup deadline exceeded"})
    finally:
        try:
            # KILL_ON_JOB_CLOSE remains a final fallback even if termination/query failed.
            job.close()
        except OSError as exc:
            errors.append({"phase": "close", "winerror": getattr(exc, "winerror", None)})
    confirmed = accounting.get("active_processes") == 0 and process.returncode is not None
    return {
        "backend": "windows_job_object",
        "kill_on_close": True,
        "breakaway_allowed": False,
        "assigned_before_resume": job.assigned and job.resumed,
        "process_resumed": job.resumed,
        "cleanup_confirmed": confirmed and not errors,
        "job_handle_closed": job.handle is None,
        **accounting,
        "errors": errors,
    }


async def _drain_readers(readers: list[asyncio.Task[None]]) -> bool:
    if not readers:
        return True
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*readers, return_exceptions=True), timeout=2
        )
        return not any(isinstance(result, BaseException) for result in results)
    except TimeoutError:
        return False


async def _wait_parent_exit(process: asyncio.subprocess.Process) -> None:
    # Process.wait() can await inherited pipe handles after the parent exits.
    # Watching returncode lets us clean surviving group members immediately.
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    handle: asyncio.Handle | None = None

    def check() -> None:
        nonlocal handle
        if process.returncode is not None:
            finished.set()
        else:
            handle = loop.call_later(0.025, check)

    check()
    try:
        await finished.wait()
    finally:
        if handle is not None:
            handle.cancel()


async def execute_command(
    argv: list[str],
    cwd: Path,
    *,
    config: Any = None,
    artifacts_dir: Path | None = None,
    timeout_seconds: float | None = None,
    environment_overrides: Mapping[str, str] | None = None,
) -> JSONDict:
    """Execute an already-authorized argv and return a complete tool payload."""
    timeout = timeout_seconds or float(getattr(config, "default_timeout_seconds", 60))
    maximum = float(getattr(config, "max_timeout_seconds", 300))
    if timeout <= 0 or timeout > maximum:
        raise ToolError("INVALID_ARGUMENTS", f"timeout_seconds must be between 0 and {maximum}.")
    output_limit = int(getattr(config, "max_output_bytes_per_stream", 1_048_576))
    artifact_limit = int(getattr(config, "max_artifact_bytes_per_stream", 8_388_608))
    if artifacts_dir is not None:
        await asyncio.to_thread(artifacts_dir.mkdir, parents=True, exist_ok=True)
    stdout = _Capture(output_limit, artifact_limit, artifacts_dir, "stdout")
    stderr = _Capture(output_limit, artifact_limit, artifacts_dir, "stderr")
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    process: asyncio.subprocess.Process | None = None
    readers: list[asyncio.Task[None]] = []
    timed_out = False
    cancelled = False
    output_incomplete = False
    job: Win32Job | None = None
    cleanup: JSONDict = {
        "backend": "windows_job_object" if os.name == "nt" else "posix_process_group",
        "cleanup_confirmed": False,
        "process_resumed": False,
    }
    cleanup_done = False
    containment_failure: JSONDict | None = None
    spawn: asyncio.Task[asyncio.subprocess.Process] | None = None

    async def finish_process() -> None:
        nonlocal cleanup_done, cleanup
        if cleanup_done or process is None:
            return
        if job is not None:
            cleanup = await _cleanup_windows(process, job)
        else:
            await _terminate_tree(process)
            cleanup = {"backend": "posix_process_group", "cleanup_confirmed": True}
        cleanup_done = True

    try:
        if os.name == "nt":
            try:
                job = Win32Job()
            except OSError as exc:
                raise ToolError(
                    "PROCESS_CONTAINMENT_FAILED",
                    "Windows Job Object could not be established; command was not started.",
                    {
                        "winerror": getattr(exc, "winerror", None),
                        "process_resumed": False,
                        "reason": str(exc),
                    },
                ) from exc
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=controlled_environment(environment_overrides),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **options,
            )
        )
        process = await asyncio.shield(spawn)
        assert process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.create_task(stdout.read(process.stdout)),
            asyncio.create_task(stderr.read(process.stderr)),
        ]
        if job is not None:
            try:
                job.attach_and_resume(process.pid)
            except OSError as exc:
                raise ToolError(
                    "PROCESS_CONTAINMENT_FAILED",
                    "Suspended command could not be safely assigned and resumed; execution refused.",
                    {
                        "winerror": getattr(exc, "winerror", None),
                        "process_resumed": job.resumed,
                        "reason": str(exc),
                    },
                ) from exc
        try:
            await asyncio.wait_for(_wait_parent_exit(process), timeout=timeout)
        except TimeoutError:
            timed_out = True
        await _wait_cleanup(finish_process())
        # Kill every owned member before draining inherited pipe handles.
        output_incomplete = not await _drain_readers(readers)
    except ToolError as exc:
        if exc.code != "PROCESS_CONTAINMENT_FAILED":
            raise
        containment_failure = exc.as_data()
        await _wait_cleanup(finish_process())
        output_incomplete = not await _wait_cleanup(_drain_readers(readers))
    except asyncio.CancelledError:
        cancelled = True
        if process is None and spawn is not None:
            process = await _wait_cleanup(spawn)
        await _wait_cleanup(finish_process())
        if readers:
            output_incomplete = not await _wait_cleanup(_drain_readers(readers))
    finally:
        if process is None and spawn is not None and spawn.done() and not spawn.cancelled():
            with contextlib.suppress(OSError):
                process = spawn.result()
        if process is not None and not cleanup_done:
            await _wait_cleanup(finish_process())
        if job is not None and job.handle is not None:
            job.close()
        stdout.close()
        stderr.close()
    artifacts = [item for item in (stdout.artifact(), stderr.artifact()) if item is not None]
    exit_code = process.returncode if process is not None else None
    code = (
        "PROCESS_CONTAINMENT_FAILED"
        if containment_failure is not None
        else "JOB_CLEANUP_UNCONFIRMED"
        if job is not None and not cleanup.get("cleanup_confirmed")
        else "COMMAND_CANCELLED"
        if cancelled
        else "COMMAND_TIMEOUT"
        if timed_out
        else "EXECUTION_UNKNOWN"
        if output_incomplete
        else (
            "COMMAND_FAILED"
            if exit_code
            else "OUTPUT_TRUNCATED"
            if stdout.total > stdout.limit or stderr.total > stderr.limit
            else None
        )
    )
    data: JSONDict = {
        "argv": argv,
        "cwd": str(cwd),
        "exit_code": exit_code,
        "stdout": stdout.text(),
        "stderr": stderr.text(),
        "timed_out": timed_out,
        "cancelled": cancelled,
        "output_incomplete": output_incomplete,
        "stdout_truncated": stdout.total > stdout.limit,
        "stderr_truncated": stderr.total > stderr.limit,
        "stdout_total_bytes": stdout.total,
        "stderr_total_bytes": stderr.total,
        "artifacts": artifacts,
        "process_cleanup": "windows_job_object" if os.name == "nt" else "posix_process_group",
        "process_management": cleanup,
    }
    if containment_failure is not None:
        data["containment_failure"] = containment_failure
    if code is not None:
        data["code"] = code
    status = (
        "error"
        if containment_failure is not None
        else "unknown"
        if job is not None and not cleanup.get("cleanup_confirmed")
        else "cancelled"
        if cancelled
        else "error"
        if timed_out or exit_code or output_incomplete
        else "ok"
    )
    content = f"exit_code={exit_code}; timed_out={timed_out}; cancelled={cancelled}\nstdout:\n{data['stdout']}\nstderr:\n{data['stderr']}"
    return {
        "status": status,
        "content": content,
        "data": data,
        "artifact_ids": tuple(item["artifact_id"] for item in artifacts),
    }


async def prepare_run_command(args: RunCommandArgs, context: ToolContext) -> PreparedTool:
    from stellarweaver.tools.registry import PreparedTool

    cwd = context.workspace.resolve(args.cwd, allow_directory=True)
    if not cwd.is_dir():
        raise ToolError("NOT_DIRECTORY", "Command cwd must be an existing directory.")
    timeout = args.timeout_seconds or float(
        getattr(context.execution, "default_timeout_seconds", 60)
    )
    maximum = float(getattr(context.execution, "max_timeout_seconds", 300))
    if timeout > maximum:
        raise ToolError("INVALID_ARGUMENTS", f"timeout_seconds cannot exceed {maximum}.")

    async def recheck() -> None:
        current = context.workspace.resolve(args.cwd, allow_directory=True)
        if current != cwd or not current.is_dir():
            raise ToolError("PATH_CHANGED", "Command cwd changed after authorization.")

    async def execute() -> JSONDict:
        return await execute_command(
            args.argv,
            cwd,
            config=context.execution,
            artifacts_dir=context.artifacts_dir,
            timeout_seconds=timeout,
        )

    return PreparedTool(
        {"argv": args.argv, "cwd": context.workspace.relative(cwd), "timeout_seconds": timeout},
        execute,
        recheck,
    )


def register_tools(registry: ToolRegistry) -> None:
    registry.register(
        "run_command",
        "Execute an authorized argv in the fixed workspace; host execution has no OS filesystem sandbox.",
        RunCommandArgs,
        prepare_run_command,
    )
