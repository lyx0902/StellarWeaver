"""One validation, authorization and journal boundary for all local tools."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import BaseModel, ValidationError

from stellarweaver.domain import JSONDict, Message, ToolCall, ToolResult, ToolStatus
from stellarweaver.policy import READ_TOOLS, PolicyEngine
from stellarweaver.session.locking import SessionBusy, workspace_write_lock
from stellarweaver.tools.workspace import ToolError, Workspace


@dataclass(frozen=True)
class ToolContext:
    workspace: Workspace
    artifacts_dir: Path
    execution: Any = None
    operation_id: str = ""


@dataclass(frozen=True)
class PreparedTool:
    invocation: JSONDict
    execute: Callable[[], Awaitable[JSONDict]]
    recheck: Callable[[], Awaitable[None]] | None = None


Prepare = Callable[[Any, ToolContext], Awaitable[PreparedTool]]
Journal = Callable[[str, JSONDict], Any]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    description: str
    arguments: type[BaseModel]
    prepare: Prepare
    schema: JSONDict | None = None
    read_only: bool = False
    remote: bool = False


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        policy: PolicyEngine,
        execution: Any = None,
        journal: Journal | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        self.workspace = Workspace(workspace)
        self.policy = policy
        self.execution = execution
        self.journal = journal
        self.artifacts_dir = artifacts_dir or self.workspace.root / ".stellarweaver" / "artifacts"
        self._tools: dict[str, RegisteredTool] = {}
        self.extension_context: Message | None = None
        self.refresh_extensions: Callable[[], Awaitable[None]] | None = None
        self.before_execute: Callable[[ToolCall, JSONDict], Awaitable[None]] | None = None
        self.after_authorize: Callable[[ToolCall, JSONDict], Awaitable[None]] | None = None
        from stellarweaver.tools import commands, files, git, search

        for module in (files, search, commands, git):
            module.register_tools(self)

    def register(
        self,
        name: str,
        description: str,
        arguments: type[BaseModel],
        prepare: Prepare,
        *,
        schema: JSONDict | None = None,
        read_only: bool | None = None,
        remote: bool = False,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Duplicate tool: {name}")
        if schema is not None:

            def external_reference(value: Any) -> bool:
                if isinstance(value, dict):
                    return any(
                        key in {"$ref", "$dynamicRef", "$recursiveRef"}
                        and isinstance(item, str)
                        and not item.startswith("#")
                        or external_reference(item)
                        for key, item in value.items()
                    )
                return isinstance(value, list) and any(external_reference(item) for item in value)

            if external_reference(schema):
                raise ValueError("External JSON Schema references are not supported")
            if len(json.dumps(schema).encode()) > 32768:
                raise ValueError("External JSON Schema exceeds 32 KiB")
            Draft202012Validator.check_schema(schema)
        self._tools[name] = RegisteredTool(
            name,
            description,
            arguments,
            prepare,
            schema,
            name in READ_TOOLS if read_only is None else read_only,
            remote,
        )

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def is_mutation(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is None or not tool.read_only

    def schemas(self) -> list[JSONDict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.schema
                    if tool.schema is not None
                    else tool.arguments.model_json_schema(),
                },
            }
            for tool in self._tools.values()
            if not (self.policy.preset == "read-only" and not tool.read_only)
        ]

    async def _record(self, event: str, data: JSONDict) -> None:
        if self.journal is not None:
            result = self.journal(event, data)
            if inspect.isawaitable(result):
                await result

    async def dispatch(self, call: ToolCall, *, operation_id: str | None = None) -> ToolResult:
        operation_id = operation_id or uuid.uuid4().hex
        started = time.monotonic()
        status: ToolStatus = "error"
        payload: JSONDict = {}
        content = ""
        artifacts: tuple[str, ...] = ()
        executing = False
        write_scope = ExitStack()
        mutating = False
        try:
            tool = self._tools.get(call.name)
            if tool is None:
                raise ToolError("UNKNOWN_TOOL", f"Unknown tool: {call.name}")
            try:
                raw = json.loads(call.arguments_json)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ToolError(
                    "INVALID_JSON", "Tool arguments are not valid JSON.", {"error": str(exc)}
                ) from exc
            try:
                if tool.schema is not None:
                    Draft202012Validator(tool.schema).validate(raw)
                args = tool.arguments.model_validate(raw)
            except SchemaValidationError as exc:
                raise ToolError(
                    "INVALID_ARGUMENTS",
                    "Tool arguments do not match the external JSON schema.",
                    {"path": [str(item) for item in exc.absolute_path], "validator": exc.validator},
                ) from exc
            except ValidationError as exc:
                raise ToolError(
                    "INVALID_ARGUMENTS",
                    "Tool arguments do not match the strict schema.",
                    {"errors": exc.errors(include_context=False, include_input=False)},
                ) from exc
            mutating = not tool.read_only
            if mutating:
                write_scope.enter_context(workspace_write_lock(self.workspace.root))
            context = ToolContext(self.workspace, self.artifacts_dir, self.execution, operation_id)
            prepared = await tool.prepare(args, context)
            invocation = {
                **prepared.invocation,
                "tool": call.name,
                "workspace": str(self.workspace.root),
                "call_id": call.call_id,
                "operation_id": operation_id,
                "arguments_json": call.arguments_json,
                "remote": tool.remote,
                "confirmed_read_only": tool.remote and tool.read_only,
            }
            if self.before_execute is not None:
                await self.before_execute(call, invocation)
            decision = await self.policy.decide(invocation)
            await self._record("tool.authorization", {**invocation, **decision})
            if decision["decision"] != "allow":
                status = "blocked" if decision["decision"] == "blocked" else "denied"
                payload = {
                    "code": "PERMISSION_DENIED",
                    "message": decision["reason"],
                    "retryable": False,
                    "details": {"decision": decision["decision"]},
                }
                content = decision["reason"]
            else:
                if self.after_authorize is not None:
                    await self.after_authorize(call, invocation)
                if prepared.recheck is not None:
                    await prepared.recheck()
                await self._record("tool.started", invocation)
                executing = True
                outcome = await prepared.execute()
                status = cast(ToolStatus, outcome.get("status", "ok"))
                if status not in {"ok", "error", "denied", "cancelled", "blocked", "unknown"}:
                    raise RuntimeError("Tool returned an invalid status")
                payload = outcome.get("data", {})
                content = outcome.get("content", json.dumps(payload, ensure_ascii=False))
                artifacts = tuple(outcome.get("artifact_ids", ()))
        except SessionBusy as exc:
            status = "blocked"
            payload = ToolError("WORKSPACE_BUSY", str(exc)).as_data()
            content = f"WORKSPACE_BUSY: {exc}"
        except ToolError as exc:
            payload = exc.as_data()
            content = f"{exc.code}: {exc.message}"
        except (OSError, UnicodeError) as exc:
            if executing and mutating:
                status = "unknown"
            code = "PERMISSION_DENIED" if isinstance(exc, PermissionError) else "IO_ERROR"
            payload = ToolError(code, str(exc)).as_data()
            content = f"{code}: {exc}"
        except asyncio.CancelledError:
            status = "cancelled"
            payload = ToolError("COMMAND_CANCELLED", "Tool was cancelled.").as_data()
            content = "Tool was cancelled."
        except Exception as exc:
            status = "unknown"
            payload = ToolError("EXECUTION_UNKNOWN", f"{type(exc).__name__}: {exc}").as_data()
            content = f"EXECUTION_UNKNOWN: {type(exc).__name__}: {exc}"
        result = ToolResult(
            call.call_id,
            operation_id,
            call.name,
            status,
            content,
            payload,
            artifacts,
            int((time.monotonic() - started) * 1000),
        )
        try:
            await self._record(
                "tool.finished",
                {
                    "call_id": call.call_id,
                    "operation_id": operation_id,
                    "tool_name": call.name,
                    "status": status,
                    "content": content,
                    "data": payload,
                    "artifact_ids": list(artifacts),
                    "duration_ms": result.duration_ms,
                },
            )
        finally:
            write_scope.close()
        return result
