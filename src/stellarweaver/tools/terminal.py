"""Container-only terminal tool (Phase 3a single-bash MVP for Harbor / Terminal-Bench).

This tool is strictly bound to isolated persistent containers under `policy.preset == "benchmark"`
and `PolicyEngine(..., allow_terminal_container=True)`. It must never be registered in interactive
CLI/Web sessions or `swe-python` profiles.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stellarweaver.tools.commands import NONINTERACTIVE_ENV
from stellarweaver.tools.workspace import ToolError

if TYPE_CHECKING:
    from stellarweaver.domain import JSONDict
    from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry


@dataclass(frozen=True)
class TerminalExecResult:
    """Normalized command outcome matching Harbor's ExecResult shape."""

    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    timed_out: bool = False


@runtime_checkable
class TerminalExecutor(Protocol):
    """Minimal contract satisfied by Harbor's BaseEnvironment.exec()."""

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> Any: ...


class BashArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    command: str = Field(min_length=1, max_length=131_072)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    timeout_seconds: int | None = Field(default=None, ge=1, le=1800)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if "\x00" in value or not value.strip():
            raise ValueError("command must be non-empty and cannot contain NUL bytes")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("cwd cannot contain NUL bytes")
        return value


def _bound_stream(
    raw_text: str,
    *,
    limit_bytes: int,
    artifacts_dir: Path | None,
    stream_name: str,
) -> tuple[str, bool, int, JSONDict | None]:
    encoded = raw_text.encode("utf-8", errors="replace")
    total = len(encoded)
    if total <= limit_bytes:
        return raw_text, False, total, None

    head_size = max(1, int(limit_bytes * 0.35))
    tail_size = max(1, limit_bytes - head_size)
    marker = b"\n[... OUTPUT_TRUNCATED: see artifact ...]\n"
    truncated = (encoded[:head_size] + marker + encoded[-tail_size:]).decode(
        "utf-8", errors="replace"
    )
    artifact: JSONDict | None = None
    if artifacts_dir is not None:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / f"{uuid.uuid4().hex}.{stream_name}.txt"
        artifact_path.write_bytes(encoded[: 8 * 1024 * 1024])
        artifact = {
            "artifact_id": artifact_path.name,
            "path": str(artifact_path),
            "total_bytes": total,
            "saved_bytes": min(total, 8 * 1024 * 1024),
            "quota_exceeded": total > 8 * 1024 * 1024,
        }
    return truncated, True, total, artifact


def _coerce_exec_result(raw: Any) -> TerminalExecResult:
    if isinstance(raw, TerminalExecResult):
        return raw
    stdout = getattr(raw, "stdout", None)
    stderr = getattr(raw, "stderr", None)
    return_code = getattr(raw, "return_code", None)
    if return_code is None:
        return_code = getattr(raw, "exit_code", 0)
    timed_out = bool(getattr(raw, "timed_out", False))
    return TerminalExecResult(
        stdout="" if stdout is None else str(stdout),
        stderr="" if stderr is None else str(stderr),
        return_code=int(return_code) if return_code is not None else 0,
        timed_out=timed_out,
    )


def register_terminal_tools(
    registry: ToolRegistry,
    executor: TerminalExecutor,
    *,
    default_env: dict[str, str] | None = None,
) -> None:
    """Register the container-only `bash` tool for `terminal-minimal` benchmark runs."""
    if registry.policy.preset != "benchmark" or not registry.policy.allow_terminal_container:
        raise ValueError(
            "Container terminal tools require policy.preset == 'benchmark' and allow_terminal_container=True"
        )
    if not registry.command_boundary:
        raise ValueError("Container terminal tools require a non-empty command_boundary")

    base_env = {**NONINTERACTIVE_ENV, **(default_env or {})}

    async def prepare_bash(args: BashArgs, context: ToolContext) -> PreparedTool:
        from stellarweaver.tools.registry import PreparedTool

        default_timeout = int(getattr(context.execution, "default_timeout_seconds", 120))
        max_timeout = int(getattr(context.execution, "max_timeout_seconds", 1800))
        timeout = args.timeout_seconds if args.timeout_seconds is not None else default_timeout
        if timeout <= 0 or timeout > max_timeout:
            raise ToolError("INVALID_ARGUMENTS", f"timeout_seconds must be between 1 and {max_timeout}.")

        output_limit = int(getattr(context.execution, "max_output_bytes_per_stream", 131_072))

        async def execute() -> JSONDict:
            timed_out = False
            exec_error: str | None = None
            result = TerminalExecResult()
            try:
                raw = await asyncio.wait_for(
                    executor.exec(
                        args.command,
                        cwd=args.cwd,
                        env=dict(base_env),
                        timeout_sec=timeout,
                    ),
                    timeout=timeout + 15,
                )
                result = _coerce_exec_result(raw)
                timed_out = result.timed_out
            except TimeoutError:
                timed_out = True
                result = TerminalExecResult(
                    stdout="",
                    stderr=f"Command timed out after {timeout}s inside container.",
                    return_code=-1,
                    timed_out=True,
                )
            except RuntimeError as exc:
                if "timed out" in str(exc).lower():
                    timed_out = True
                    result = TerminalExecResult(
                        stdout="",
                        stderr=str(exc) or f"Command timed out after {timeout}s inside container.",
                        return_code=-1,
                        timed_out=True,
                    )
                else:
                    exec_error = f"{type(exc).__name__}: {exc}"
                    result = TerminalExecResult(stdout="", stderr=exec_error, return_code=-1)
            except Exception as exc:
                exec_error = f"{type(exc).__name__}: {exc}"
                result = TerminalExecResult(stdout="", stderr=exec_error, return_code=-1)

            stdout_text, stdout_trunc, stdout_total, stdout_art = _bound_stream(
                result.stdout,
                limit_bytes=output_limit,
                artifacts_dir=context.artifacts_dir,
                stream_name="stdout",
            )
            stderr_text, stderr_trunc, stderr_total, stderr_art = _bound_stream(
                result.stderr,
                limit_bytes=output_limit,
                artifacts_dir=context.artifacts_dir,
                stream_name="stderr",
            )
            artifacts = [item for item in (stdout_art, stderr_art) if item is not None]
            exit_code: int | None = None if (timed_out or exec_error) else result.return_code
            code: str | None = (
                "COMMAND_TIMEOUT"
                if timed_out
                else "TERMINAL_EXEC_ERROR"
                if exec_error is not None
                else "COMMAND_FAILED"
                if exit_code
                else "OUTPUT_TRUNCATED"
                if stdout_trunc or stderr_trunc
                else None
            )
            data: JSONDict = {
                "command": args.command,
                "cwd": args.cwd,
                "exit_code": exit_code,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "timed_out": timed_out,
                "cancelled": False,
                "stdout_truncated": stdout_trunc,
                "stderr_truncated": stderr_trunc,
                "stdout_total_bytes": stdout_total,
                "stderr_total_bytes": stderr_total,
                "artifacts": artifacts,
                "process_cleanup": "harbor_persistent_container",
            }
            if code is not None:
                data["code"] = code
            if exec_error is not None:
                data["error"] = exec_error
            status = "error" if (timed_out or exec_error is not None or exit_code) else "ok"
            content = (
                f"exit_code={exit_code}; timed_out={timed_out}\n"
                f"stdout:\n{stdout_text}\nstderr:\n{stderr_text}"
            )
            return {
                "status": status,
                "content": content,
                "data": data,
                "artifact_ids": tuple(item["artifact_id"] for item in artifacts),
            }

        invocation: JSONDict = {
            "command": args.command,
            "cwd": args.cwd or ".",
            "timeout_seconds": timeout,
        }
        return PreparedTool(invocation, execute, command_executor="sandbox")

    registry.register(
        "bash",
        "Execute a bash command inside the persistent isolated task container. "
        "Non-interactive defaults (DEBIAN_FRONTEND=noninteractive, GIT_TERMINAL_PROMPT=0, "
        "PIP_NO_INPUT=1, CI=true) are pre-set. Pass a larger timeout_seconds (up to 1800) for "
        "compilations, package installations, or long-running jobs. Background daemons started "
        "with nohup/... & persist across calls inside the container.",
        BashArgs,
        prepare_bash,
        read_only=False,
    )
