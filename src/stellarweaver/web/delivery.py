"""Bounded evidence for local repair runs. No shell or network calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from stellarweaver.domain import JSONDict, ToolResult
from stellarweaver.tools.workspace import Workspace
from stellarweaver.web.isolation import snapshot_hashes

MAX_ENTRIES = 6000
MAX_BYTES = 32 * 1024 * 1024
MAX_PATCH = 24000
MAX_OUTPUT = 6000


def content_snapshot(root: Path, state_dir: Path) -> dict[str, str] | None:
    """Hash regular, non-ignored worktree bytes; refuse incomplete observations."""
    found: dict[str, str] = {}
    total = visited = 0

    def walk_error(error: OSError) -> None:
        raise error

    try:
        workspace = Workspace(root)
        workspace.protect(state_dir)
        for parent, dirs, files in os.walk(root, onerror=walk_error):
            dirs.sort()
            files.sort()
            for name in [*dirs, *files]:
                visited += 1
                if visited > MAX_ENTRIES:
                    return None
                candidate = Path(parent) / name
                relative = candidate.relative_to(root).as_posix()
                if workspace.is_ignored(relative, is_dir=name in dirs):
                    if name in dirs:
                        dirs.remove(name)
                    continue
                info = candidate.lstat()
                if candidate.is_symlink() or (
                    hasattr(candidate, "is_junction") and candidate.is_junction()
                ):
                    return None
                if name in dirs:
                    continue
                if not stat.S_ISREG(info.st_mode) or total + info.st_size > MAX_BYTES:
                    return None
                with candidate.open("rb") as stream:
                    data = stream.read(info.st_size + 1)
                    observed = os.fstat(stream.fileno())
                if (observed.st_size, observed.st_mtime_ns) != (
                    info.st_size,
                    info.st_mtime_ns,
                ) or len(data) != info.st_size:
                    return None
                total += len(data)
                found[relative] = hashlib.sha256(data).hexdigest()
        return found
    except (OSError, ValueError, RuntimeError):
        return None


def manifest_sha(files: dict[str, str] | None) -> str | None:
    if files is None:
        return None
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def clip(text: object, n: int) -> str:
    value = str(text)
    return value[:n] + ("\n[truncated]" if len(value) > n else "")


@dataclass
class Delivery:
    workspace: Path
    state_dir: Path
    baseline: dict[str, str] | None = None
    test_inputs: dict[str, str | None] = field(default_factory=dict)
    patches: list[JSONDict] = field(default_factory=list)
    checks: list[JSONDict] = field(default_factory=list)
    edits: int = 0
    isolated: bool = False
    required_commands: tuple[tuple[str, ...], ...] = ()

    def snapshot(self) -> dict[str, str] | None:
        if self.isolated:
            return snapshot_hashes(self.workspace)
        return content_snapshot(self.workspace, self.state_dir)

    def begin(self) -> None:
        self.baseline = self.snapshot()

    def started(self, payload: JSONDict) -> None:
        if payload.get("tool") in {"run_command", "run_tests", "check_syntax", "run_linter"}:
            self.test_inputs[payload["operation_id"]] = manifest_sha(self.snapshot())

    def result(self, result: ToolResult) -> None:
        data = result.data
        if result.tool_name in {"edit_file", "create_file"} and result.status == "ok":
            self.edits += 1
            self.patches.append(
                {
                    **{k: data[k] for k in ("path", "before_sha256", "after_sha256") if k in data},
                    "operation_id": result.operation_id,
                    "diff": clip(data.get("diff", ""), MAX_PATCH),
                }
            )
        if (
            result.tool_name in {"run_command", "run_tests", "check_syntax", "run_linter"}
            and "exit_code" in data
        ):
            verified = self.test_inputs.pop(result.operation_id, None)
            after = manifest_sha(self.snapshot())
            summary = re.findall(r"^Ran (\d+) tests? in ", data.get("stderr", ""), re.MULTILINE)
            self.checks.append(
                {
                    "operation_id": result.operation_id,
                    "tool": result.tool_name,
                    "supplemental": result.tool_name != "run_command",
                    "diagnostic": data.get("diagnostic"),
                    "edit_generation": self.edits,
                    "exit_code": data["exit_code"],
                    "status": result.status,
                    "code": data.get("code"),
                    "test_count": int(summary[-1]) if summary else None,
                    "cleanup_confirmed": data.get("process_management", {}).get(
                        "cleanup_confirmed"
                    ),
                    "argv": data.get("argv"),
                    "cwd": ".",
                    "stdout": clip(data.get("stdout", ""), MAX_OUTPUT),
                    "stderr": clip(data.get("stderr", ""), MAX_OUTPUT),
                    "input_sha256": verified,
                    "unchanged_during_check": verified is not None and verified == after,
                }
            )

    def report(self, run_id: str, outcome: JSONDict) -> JSONDict:
        current = self.snapshot()
        comparable = current is not None and self.baseline is not None
        paths = (
            sorted(
                path
                for path in self.baseline.keys() | current.keys()
                if self.baseline.get(path) != current.get(path)
            )
            if self.baseline is not None and current is not None
            else []
        )
        final_sha = manifest_sha(current)
        checks = []
        for check in self.checks:
            fresh = (
                final_sha is not None
                and check["input_sha256"] == final_sha
                and check["unchanged_during_check"]
                and check["edit_generation"] == self.edits
            )
            if check["status"] == "cancelled":
                state = "cancelled"
            elif check["code"] == "COMMAND_TIMEOUT":
                state = "timed_out"
            elif check["status"] == "unknown" or check["exit_code"] is None:
                state = "unconfirmed"
            elif check["test_count"] == 0:
                state = "unconfirmed"
            elif check["exit_code"] != 0 or check["status"] != "ok":
                state = "failed"
            elif not check["test_count"] and not (
                self.isolated and check["cleanup_confirmed"] and check["input_sha256"]
            ):
                state = "unconfirmed"
            else:
                state = "passed"
            effective = "stale" if not fresh and state in {"passed", "failed"} else state
            checks.append({**check, "state": effective, "observed": state})
        if self.isolated:
            latest = {
                tuple(check["argv"] or ()): check["state"]
                for check in checks
                if not check["supplemental"]
            }
            verification = next(
                (
                    state
                    for state in ("failed", "timed_out", "cancelled", "unconfirmed", "stale")
                    if state in latest.values()
                ),
                "passed" if latest else "unconfirmed" if checks else "not_run",
            )
            if verification == "passed" and not latest.keys() >= set(self.required_commands):
                verification = "unconfirmed"
        else:
            verification = checks[-1]["state"] if checks else "not_run"
        if outcome["run_status"] != "completed" and verification == "passed":
            verification = "stale"
        return {
            "schema_version": 1,
            "run_id": run_id,
            "run_status": outcome["run_status"],
            "task_assessment": "unknown",
            "verification": verification,
            "scope": (
                "tracked and non-ignored untracked Git worktree files; excludes Git metadata and protected paths"
                if self.isolated
                else "bounded regular non-ignored worktree files; excludes Git metadata and protected paths"
            ),
            "baseline_comparable": comparable,
            "final_sha256": final_sha,
            "changed_paths": paths,
            "patches": self.patches,
            "checks": checks,
        }
