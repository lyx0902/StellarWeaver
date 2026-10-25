"""Evidence-bounded inspection and undo of built-in file modifications."""

from __future__ import annotations

import difflib
import inspect
import json
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any
from uuid import uuid4

from stellarweaver.domain import AgentEvent, JSONDict, ToolResult
from stellarweaver.policy import PolicyEngine
from stellarweaver.session.locking import SessionBusy, workspace_write_lock
from stellarweaver.session.store import SessionStore, now
from stellarweaver.tools.files import fsync_directory
from stellarweaver.tools.git import (
    GitDiffArgs,
    GitStatusArgs,
    prepare_git_diff,
    prepare_git_status,
)
from stellarweaver.tools.registry import PreparedTool, ToolContext
from stellarweaver.tools.workspace import MAX_EDIT_BYTES, ToolError, Workspace, sha256_bytes


class ChangeObservationUnknown(RuntimeError):
    """A Git observation could not finish within confirmed process containment."""

    def __init__(self, stage: str, result: JSONDict) -> None:
        self.status = str(result.get("status", "unknown"))
        details = result.get("data", {})
        self.code = str(details.get("code") or "CHANGE_OBSERVATION_UNKNOWN")
        self.data: JSONDict = {"stage": stage, **result}
        super().__init__(f"Git observation {stage} stopped: {self.code} ({self.status})")


class ChangesManager:
    def __init__(
        self,
        workspace: Path,
        store: SessionStore,
        policy: PolicyEngine,
        *,
        on_event: Callable[[AgentEvent], Any] | None = None,
    ) -> None:
        self.workspace = Workspace(workspace)
        if self.workspace.root != store.workspace:
            raise ValueError("Change manager workspace must match the session workspace")
        self.workspace.protect(store.directory)
        self.store = store
        self.policy = policy
        self.on_event = on_event

    async def _emit(self, kind: str, payload: JSONDict) -> None:
        event = self.store.append_event(kind, payload)
        if self.on_event is not None:
            result = self.on_event(event)
            if inspect.isawaitable(result):
                await result

    async def _check_observation(self, stage: str, result: JSONDict) -> None:
        if result.get("status") in {"unknown", "cancelled"} or result.get("data", {}).get(
            "code"
        ) in {"JOB_CLEANUP_UNCONFIRMED", "PROCESS_CONTAINMENT_FAILED"}:
            failure = ChangeObservationUnknown(stage, result)
            await self._emit("changes.observation_unknown", failure.data)
            raise failure

    async def _observe(
        self, stage: str, prepare: Callable[[], Awaitable[PreparedTool]]
    ) -> JSONDict:
        try:
            result: JSONDict = await (await prepare()).execute()
        except ToolError as exc:
            result = {"status": "error", "data": exc.as_data()}
        except OSError:
            result = {"status": "error", "data": {}}
        await self._check_observation(stage, result)
        return result

    def _baseline(self) -> JSONDict | None:
        for event in self.store.load_events():
            if event.type == "changes.baseline":
                return event.payload
        return None

    def _safe(self, relative: str) -> bool:
        try:
            self.workspace.resolve(relative, allow_directory=True, must_exist=False)
            return not self.workspace.is_ignored(relative)
        except (OSError, ToolError, ValueError):
            return False

    def _file_state(self, relative: str) -> JSONDict:
        try:
            path = self.workspace.resolve(relative, allow_directory=True, must_exist=False)
            raw = self.workspace.root / relative
            if raw.is_symlink() or any(
                parent.is_symlink()
                for parent in raw.parents
                if parent != self.workspace.root and parent.is_relative_to(self.workspace.root)
            ):
                return {"state": "symlink"}
            if not path.exists():
                return {"state": "missing", "sha256": None}
            info = path.stat()
            if not path.is_file():
                return {"state": "not_file"}
            result: JSONDict = {
                "state": "file",
                "mode": stat.S_IMODE(info.st_mode),
                "byte_length": info.st_size,
            }
            if info.st_size <= 8 * MAX_EDIT_BYTES:
                with path.open("rb") as stream:
                    raw_bytes = stream.read(8 * MAX_EDIT_BYTES + 1)
                if len(raw_bytes) <= 8 * MAX_EDIT_BYTES:
                    result["sha256"] = sha256_bytes(raw_bytes)
                else:
                    result["hash_unavailable"] = "File grew beyond the bounded hash limit"
            else:
                result["hash_unavailable"] = "File exceeds the bounded 8 MiB baseline hash limit"
            return result
        except (OSError, ToolError, ValueError) as exc:
            return {"state": "unavailable", "reason": type(exc).__name__}

    async def _git(self) -> JSONDict:
        context = ToolContext(self.workspace, self.store.artifacts_dir)
        status = await self._observe("status", lambda: prepare_git_status(GitStatusArgs(), context))
        if status["status"] != "ok":
            return {
                "git_available": False,
                "status": {},
                "files": {},
                "staged_diff": "",
                "unstaged_diff": "",
                "limitations": [
                    "Git status is unavailable; shell changes cannot be fully classified."
                ],
            }
        data = status["data"]
        groups = {
            name: [path for path in data[name] if self._safe(path)]
            for name in ("staged", "unstaged", "untracked")
        }
        renames = [
            {"from": entry["original_path"], "to": entry["path"]}
            for entry in data.get("entries", [])
            if entry.get("original_path")
            and "R" in (entry.get("index_status"), entry.get("worktree_status"))
            and self._safe(entry["path"])
            and self._safe(entry["original_path"])
        ]
        paths = sorted(
            {path for group in groups.values() for path in group}
            | {item["from"] for item in renames}
        )
        files = {path: self._file_state(path) for path in paths}
        patches: dict[str, str] = {}
        limitations: list[str] = []
        if data.get("status_incomplete"):
            limitations.append("Git status was truncated; path classification is incomplete.")
        for staged, label in ((True, "staged_diff"), (False, "unstaged_diff")):
            result = await self._observe(
                label, partial(prepare_git_diff, GitDiffArgs(staged=staged), context)
            )
            patches[label] = str(result["data"].get("diff", ""))
            if result["status"] != "ok" or result["data"].get("stdout_truncated"):
                limitations.append(f"{label} is unavailable or truncated.")
        return {
            "git_available": True,
            "status": groups,
            "files": files,
            "renames": renames,
            **patches,
            "limitations": limitations,
        }

    async def capture_baseline(self) -> JSONDict:
        existing = self._baseline()
        if existing is not None:
            return existing
        with workspace_write_lock(self.workspace.root):
            baseline = await self._git()
            baseline.update(
                {
                    "workspace": str(self.workspace.root),
                    "captured_at": now(),
                    "capture_phase": "startup"
                    if not self.store.load_results()
                    else "after_recorded_operations",
                }
            )
            if baseline["capture_phase"] != "startup":
                baseline["limitations"].append(
                    "No startup baseline was saved; this baseline was captured after recorded operations."
                )
            await self._emit("changes.baseline", baseline)
            return baseline

    def _edits(self) -> list[tuple[ToolResult, JSONDict]]:
        undone = {
            event.payload["target_operation_id"]
            for event in self.store.load_events()
            if event.type == "changes.undo.completed" and event.payload.get("status") == "ok"
        }
        intents = {
            str(row[0]): json.loads(row[1] or "{}")
            for row in self.store.connection.execute(
                "SELECT operation_id,intent_json FROM tool_executions"
            )
        }
        return [
            (result, {**intents.get(result.operation_id, {}), **result.data})
            for result in self.store.load_results()
            if result.tool_name in {"create_file", "edit_file"}
            and result.operation_id not in undone
            and result.status in {"ok", "unknown"}
        ]

    async def diff(self) -> str:
        baseline = await self.capture_baseline()
        current = await self._git()
        edits = self._edits()
        lines = [
            "User baseline at session start",
            f"Captured: {baseline['captured_at']} ({baseline['capture_phase']})",
            json.dumps(baseline.get("status", {}), ensure_ascii=False, sort_keys=True),
        ]
        for label in ("staged_diff", "unstaged_diff"):
            if baseline.get(label):
                lines.extend([f"Baseline {label}:", str(baseline[label])])
        lines.append(
            "Agent built-in file changes (recorded operations; shell changes are excluded)"
        )
        if not edits:
            lines.append("No active built-in edits.")
        latest: dict[str, JSONDict] = {}
        for result, metadata in edits:
            path = str(metadata.get("path", "<unknown>"))
            latest[path] = {**metadata, "_result_status": result.status}
            lines.append(f"{result.operation_id} {result.tool_name} {path} [{result.status}]")
            lines.append(
                str(
                    metadata.get(
                        "diff",
                        metadata.get(
                            "preview", "Diff artifact unavailable; inspect the recorded hashes."
                        ),
                    )
                )
            )
        others: list[str] = []
        baseline_files: JSONDict = baseline.get("files", {})
        current_files: JSONDict = current.get("files", {})
        observations: JSONDict = {}
        # Git omits removed untracked paths and tracked paths restored to clean.
        # Observe the union so those baseline changes cannot disappear from /diff.
        for path in sorted(set(baseline_files) | set(current_files) | set(latest)):
            observed = current_files.get(path)
            if observed is None:
                observed = self._file_state(path)
            observations[path] = observed
            expected = latest.get(path)
            if (
                expected is not None
                and expected.get("_result_status") == "ok"
                and observed.get("state") == "file"
                and observed.get("sha256") == expected.get("after_sha256")
                and observed.get("mode") == expected.get("mode")
            ):
                continue
            original = baseline_files.get(path)
            if original is not None and observed == original:
                continue
            others.append(path)
        lines.append("Other current changes (unattributed; review shell, formatter, or user edits)")
        if others:
            for path in others:
                lines.extend(
                    [
                        path,
                        "  baseline="
                        + json.dumps(baseline_files.get(path), ensure_ascii=False, sort_keys=True),
                        "  current="
                        + json.dumps(observations[path], ensure_ascii=False, sort_keys=True),
                    ]
                )
        else:
            lines.append("No additional changes observed within the captured Git scope.")
        for rename in current.get("renames", []):
            if rename not in baseline.get("renames", []):
                lines.append(f"Current Git rename: {rename['from']} -> {rename['to']}")
        if (
            current.get("git_available")
            and baseline.get("git_available")
            and current.get("staged_diff") != baseline.get("staged_diff")
        ):
            lines.extend(
                [
                    "Current staged diff changed since the baseline:",
                    str(current.get("staged_diff", "")),
                ]
            )
        if others:
            context = ToolContext(self.workspace, self.store.artifacts_dir)
            patch = await self._observe(
                "current_paths_diff",
                lambda: prepare_git_diff(GitDiffArgs(paths=sorted(set(others))), context),
            )
            if patch.get("status") == "ok":
                lines.append(str(patch.get("content", "")))
            else:
                lines.append("Current patch unavailable; read the listed paths before editing.")
        lines.extend(
            str(item)
            for item in [*baseline.get("limitations", []), *current.get("limitations", [])]
        )
        return "\n".join(lines)

    @staticmethod
    def _bounded_bytes(path: Path) -> bytes:
        with path.open("rb") as stream:
            content = stream.read(MAX_EDIT_BYTES + 1)
        if len(content) > MAX_EDIT_BYTES:
            raise ToolError(
                "UNDO_CONFLICT", "File grew beyond the edit limit; contents are preserved."
            )
        return content

    def _checked_target(self, metadata: JSONDict) -> tuple[Path, bytes | None, int]:
        relative = metadata.get("path")
        after = metadata.get("after_sha256")
        mode = metadata.get("mode")
        if not isinstance(relative, str) or not isinstance(after, str) or not isinstance(mode, int):
            raise ToolError(
                "UNDO_METADATA_MISSING", "Undo requires recorded path, hashes, and permissions."
            )
        path = self.workspace.resolve(relative)
        raw = self.workspace.root / relative
        if raw.is_symlink() or any(
            parent.is_symlink()
            for parent in raw.parents
            if parent != self.workspace.root and parent.is_relative_to(self.workspace.root)
        ):
            raise ToolError("UNDO_PATH_CHANGED", "Undo refuses symlink targets.")
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != mode:
            raise ToolError(
                "UNDO_MODE_CHANGED", "File type or permissions changed after the agent edit."
            )
        if path.stat().st_size > MAX_EDIT_BYTES or sha256_bytes(self._bounded_bytes(path)) != after:
            raise ToolError(
                "UNDO_CONFLICT",
                "Current contents differ from the agent after hash; user changes are preserved.",
            )
        before = metadata.get("before_sha256")
        if before is None:
            return path, None, mode
        identifier = metadata.get("before_artifact_id")
        if not isinstance(identifier, str):
            raise ToolError("UNDO_BACKUP_MISSING", "No before snapshot was recorded for this edit.")
        backup = Path(identifier)
        if not backup.is_absolute():
            backup = self.store.artifacts_dir / backup
        if backup.is_symlink() or not backup.resolve(strict=True).is_relative_to(
            self.store.artifacts_dir.resolve()
        ):
            raise ToolError(
                "UNDO_BACKUP_INVALID", "Before snapshot leaves the session artifact directory."
            )
        if backup.stat().st_size > MAX_EDIT_BYTES:
            raise ToolError("UNDO_BACKUP_INVALID", "Before snapshot exceeds the edit size limit.")
        previous = self._bounded_bytes(backup)
        if sha256_bytes(previous) != before:
            raise ToolError(
                "UNDO_BACKUP_CHANGED", "Before snapshot hash does not match the original journal."
            )
        return path, previous, mode

    def _restore(self, path: Path, content: bytes | None, mode: int) -> None:
        if content is None:
            path.unlink()
            fsync_directory(path.parent)
            return
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".stellarweaver-undo-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            fsync_directory(path.parent)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _pending_undo(self) -> JSONDict | None:
        intents: dict[str, JSONDict] = {}
        for event in self.store.load_events():
            if event.type == "changes.undo.started":
                intents[event.payload["undo_id"]] = event.payload
            elif event.type == "changes.undo.completed":
                intents.pop(event.payload["undo_id"], None)
        return next(reversed(intents.values()), None) if intents else None

    async def undo(self) -> JSONDict:
        identifier = uuid4().hex
        target: str | None = None
        started = False
        try:
            with workspace_write_lock(self.workspace.root):
                pending = self._pending_undo()
                if pending is not None:
                    status, content, details = self.store._inspect_undo_state(pending)
                    result = {
                        **pending,
                        **details,
                        "status": status,
                        "message": content,
                        "requires_attention": status != "ok",
                    }
                    await self._emit("changes.undo.completed", result)
                    return result
                pending_tools = self.store.connection.execute(
                    "SELECT COUNT(*) FROM tool_executions WHERE state='started' AND result_json IS NULL"
                ).fetchone()[0]
                if pending_tools:
                    return {
                        "status": "blocked",
                        "code": "UNDO_PENDING_TOOLS",
                        "message": "Reconcile incomplete tool executions before undo.",
                    }
                edits = self._edits()
                if not edits:
                    return {
                        "status": "ok",
                        "code": "UNDO_EMPTY",
                        "message": "No active built-in edits to undo.",
                    }
                original, metadata = edits[-1]
                target = original.operation_id
                if original.status != "ok":
                    return {
                        "status": "blocked",
                        "code": "UNDO_UNKNOWN",
                        "target_operation_id": target,
                        "message": "The latest edit has unknown effects; inspect it before undo.",
                    }
                if original.tool_name == "edit_file" and not isinstance(
                    metadata.get("before_sha256"), str
                ):
                    raise ToolError(
                        "UNDO_METADATA_MISSING",
                        "An edit must have a recorded before hash; it cannot be undone as a creation.",
                    )
                if (
                    original.tool_name == "create_file"
                    and metadata.get("before_sha256") is not None
                ):
                    raise ToolError(
                        "UNDO_METADATA_MISSING", "A creation has inconsistent before metadata."
                    )
                path, previous, mode = self._checked_target(metadata)
                current = self._bounded_bytes(path)
                preview = "".join(
                    difflib.unified_diff(
                        current.decode("utf-8").splitlines(keepends=True),
                        (previous or b"").decode("utf-8").splitlines(keepends=True),
                        fromfile=f"a/{metadata['path']}",
                        tofile=f"b/{metadata['path']}",
                    )
                )
                invocation = {
                    "tool": original.tool_name,
                    "workspace": str(self.workspace.root),
                    "path": metadata["path"],
                    "operation_id": identifier,
                    "action": "undo",
                    "undo_of_operation_id": target,
                    "preview": preview,
                }
                decision = await self.policy.decide(invocation)
                await self._emit("changes.undo.authorization", {**invocation, **decision})
                if decision["decision"] != "allow":
                    return {
                        "status": "blocked" if decision["decision"] == "blocked" else "denied",
                        "code": "PERMISSION_DENIED",
                        "message": decision["reason"],
                        "target_operation_id": target,
                    }
                path, previous, mode = self._checked_target(metadata)
                intent = {
                    "undo_id": identifier,
                    "target_operation_id": target,
                    "path": metadata["path"],
                    "expected_sha256": metadata["after_sha256"],
                    "restore_sha256": metadata.get("before_sha256"),
                    "mode": mode,
                    "action": "delete_created_file"
                    if previous is None
                    else "restore_before_snapshot",
                }
                await self._emit("changes.undo.started", intent)
                started = True
                self._restore(path, previous, mode)
                result = {
                    **intent,
                    "status": "ok",
                    "code": "UNDO_COMPLETED",
                    "message": "Undid one built-in file modification; user baseline and Git index were preserved.",
                }
                await self._emit("changes.undo.completed", result)
                return result
        except SessionBusy as exc:
            return {"status": "blocked", "code": "WORKSPACE_BUSY", "message": str(exc)}
        except (OSError, ToolError, UnicodeError) as exc:
            result = {
                "undo_id": identifier,
                "target_operation_id": target,
                "status": "unknown" if started else "blocked",
                "code": "UNDO_STATE_UNKNOWN"
                if started
                else exc.code
                if isinstance(exc, ToolError)
                else "UNDO_IO_ERROR",
                "message": str(exc),
            }
            await self._emit("changes.undo.rejected", result)
            return result


__all__ = ["ChangeObservationUnknown", "ChangesManager"]
