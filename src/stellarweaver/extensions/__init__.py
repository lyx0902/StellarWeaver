"""Explicit, trust-gated skills and local MCP extensions."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import BaseModel, ConfigDict

from stellarweaver.domain import AgentEvent, JSONDict, Message
from stellarweaver.extensions.mcp import (
    ExtensionsConfig,
    MCPConnection,
    MCPFailure,
    ServerConfig,
    normalize_result,
    tool_name,
)
from stellarweaver.extensions.skills import SkillCatalog
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry
from stellarweaver.tools.workspace import ToolError, sha256_bytes


class SkillArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str


class RemoteArguments(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class ExtensionManager:
    def __init__(
        self,
        registry: ToolRegistry,
        store: SessionStore,
        config_path: Path | None = None,
        trusted: bool = False,
        user_skill_dirs: Sequence[Path] = (),
        *,
        on_event: Callable[[AgentEvent], Any] | None = None,
    ) -> None:
        self.registry = registry
        self.store = store
        self.config_path = config_path
        self.trusted = trusted
        self.user_skill_dirs = tuple(user_skill_dirs)
        self.on_event = on_event
        self._connections: list[MCPConnection] = []
        self._registered: list[str] = []
        self._remote_names: dict[str, list[str]] = {}
        self._servers: list[JSONDict] = []
        self._skills = SkillCatalog(())

    @property
    def skill_catalog(self) -> tuple[JSONDict, ...]:
        return self._skills.metadata

    @property
    def catalog(self) -> tuple[JSONDict, ...]:
        return (*self.skill_catalog, *self._servers)

    @property
    def context_message(self) -> Message | None:
        if not self.catalog and not self._skills.errors:
            return None
        return Message(
            "system",
            "Extension catalog, with attributed metadata only; no execution authority. "
            "Skills must be explicitly loaded with load_skill. MCP annotations are untrusted; "
            "all calls still use the frozen permission policy.\n"
            + json.dumps(
                {
                    "skills": self.skill_catalog,
                    "skill_errors": self._skills.errors,
                    "servers": self._servers,
                },
                ensure_ascii=False,
            ),
        )

    def _update_context(self) -> None:
        self.registry.extension_context = self.context_message

    async def _emit(self, kind: str, payload: JSONDict) -> None:
        event = self.store.append_event(kind, payload)
        if self.on_event is not None:
            value = self.on_event(event)
            if inspect.isawaitable(value):
                await value

    async def __aenter__(self) -> ExtensionManager:
        try:
            return await self._enter()
        except BaseException:
            # __aexit__ is not invoked when __aenter__ fails. Close earlier
            # servers and registrations even if a later initialization is cancelled.
            await self.aclose()
            raise

    async def _enter(self) -> ExtensionManager:
        directories: list[tuple[Path, str]] = []
        if self.trusted:
            project = self.registry.workspace.root / ".stellarweaver" / "skills"
            if (self.registry.workspace.root / ".stellarweaver").is_symlink():
                raise ValueError("Project skill directory may not use a symlink")
            directories.append((project, "trusted_project"))
        directories.extend(
            (path.expanduser().absolute(), "explicit_user") for path in self.user_skill_dirs
        )
        self._skills = SkillCatalog(directories)
        if self.skill_catalog or self._skills.errors:
            await self._emit(
                "skills.discovered",
                {"skills": list(self.skill_catalog), "errors": self._skills.errors},
            )
        if self.skill_catalog:

            async def load(args: SkillArguments, context: ToolContext) -> PreparedTool:
                async def execute() -> JSONDict:
                    data = self._skills.load(args.name)
                    await self._emit(
                        "skill.loaded",
                        {key: value for key, value in data.items() if key != "content"},
                    )
                    return {
                        "content": data["content"],
                        "data": {key: value for key, value in data.items() if key != "content"},
                    }

                return PreparedTool({"skill_name": args.name}, execute)

            self.registry.register(
                "load_skill",
                "Load one explicitly discovered skill body; grants no authority.",
                SkillArguments,
                load,
                read_only=True,
            )
            self._registered.append("load_skill")
        if self.config_path is not None:
            raw = self.config_path.read_bytes()
            if len(raw) > 65536:
                raise ValueError("Extension configuration exceeds 64 KiB")
            data = (
                tomllib.loads(raw.decode())
                if self.config_path.suffix.lower() == ".toml"
                else json.loads(raw)
            )
            config = ExtensionsConfig.model_validate_json(json.dumps(data))
            if len({item.server_id for item in config.servers}) != len(config.servers):
                raise ValueError("Extension server IDs must be unique")
            for server in config.servers:
                await self._start(server)
        self._update_context()
        self.registry.refresh_extensions = self.refresh
        return self

    async def _start(self, config: ServerConfig) -> None:
        if not self.trusted:
            self._servers.append({"server_id": config.server_id, "status": "untrusted"})
            await self._emit(
                "mcp.start_blocked",
                {"server_id": config.server_id, "reason": "Workspace trust is required"},
            )
            return
        connection: MCPConnection | None = None
        registered_before = len(self._registered)
        try:
            cwd = self.registry.workspace.resolve(config.cwd, allow_directory=True)
            if not cwd.is_dir():
                raise ToolError("INVALID_CWD", "MCP cwd must be a directory")
            invocation = {
                "tool": "mcp_start",
                "server_id": config.server_id,
                "argv": [config.command, *config.argv],
                "cwd": self.registry.workspace.relative(cwd),
                "workspace": str(self.registry.workspace.root),
                "allow_env": list(config.allow_env),
            }
            decision = await self.registry.policy.decide(invocation)
            await self._emit("mcp.start_authorization", {**invocation, **decision})
            if decision["decision"] != "allow":
                self._servers.append({"server_id": config.server_id, "status": "blocked"})
                return
            if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in config.allow_env):
                raise ValueError("Invalid allowed environment variable name")
            environment = {
                name: os.environ[name] for name in config.allow_env if name in os.environ
            }
            connection = MCPConnection(config, cwd, environment)
            await connection.start()
            tools = await connection.list_tools()
            names: list[JSONDict] = []
            for tool in tools:
                visible = tool_name(config.server_id, tool.name)
                confirmed = tool.name in config.read_only_tools
                # Server annotations alone never promote a tool into read-only mode.
                self._register_remote(
                    connection,
                    tool.name,
                    visible,
                    tool.description or "MCP tool",
                    tool.inputSchema,
                    confirmed,
                )
                names.append(
                    {"name": visible, "original_name": tool.name, "confirmed_read_only": confirmed}
                )
            self._connections.append(connection)
            self._remote_names[config.server_id] = [item["name"] for item in names]
            self._servers.append(
                {
                    "server_id": config.server_id,
                    "status": "connected",
                    "protocol_version": connection.protocol_version,
                    "tools": names,
                }
            )
            await self._emit("mcp.connected", self._servers[-1])
        except asyncio.CancelledError:
            if connection is not None:
                await connection.close()
            raise
        except Exception as exc:
            if connection is not None:
                await connection.close()
            for name in self._registered[registered_before:]:
                self.registry.unregister(name)
            del self._registered[registered_before:]
            self._servers.append(
                {
                    "server_id": config.server_id,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                }
            )
            await self._emit("mcp.failed", self._servers[-1])

    async def refresh(self) -> None:
        """Apply server list-change notifications at the next model snapshot boundary."""
        for connection in self._connections:
            if not connection.tools_changed or connection.closed:
                continue
            connection.tools_changed = False
            server_id = connection.config.server_id
            record = next(item for item in self._servers if item["server_id"] == server_id)
            try:
                tools = await connection.list_tools()
                for name in self._remote_names[server_id]:
                    self.registry.unregister(name)
                    self._registered.remove(name)
                names: list[JSONDict] = []
                for tool in tools:
                    visible = tool_name(server_id, tool.name)
                    confirmed = tool.name in connection.config.read_only_tools
                    self._register_remote(
                        connection,
                        tool.name,
                        visible,
                        tool.description or "MCP tool",
                        tool.inputSchema,
                        confirmed,
                    )
                    names.append(
                        {
                            "name": visible,
                            "original_name": tool.name,
                            "confirmed_read_only": confirmed,
                        }
                    )
                self._remote_names[server_id] = [item["name"] for item in names]
                record["tools"] = names
                await self._emit("mcp.tools_updated", {"server_id": server_id, "tools": names})
            except Exception:
                record["status"] = "failed"
                for name in self._remote_names[server_id]:
                    self.registry.unregister(name)
                await self._emit(
                    "mcp.failed",
                    {"server_id": server_id, "reason": "Tool discovery refresh failed"},
                )
            self._update_context()

    def _register_remote(
        self,
        connection: MCPConnection,
        original: str,
        visible: str,
        description: str,
        schema: JSONDict,
        confirmed: bool,
    ) -> None:
        async def prepare(args: RemoteArguments, context: ToolContext) -> PreparedTool:
            async def execute() -> JSONDict:
                try:
                    remote = await connection.call(original, args.model_dump())
                    result = normalize_result(remote)
                    if remote.structuredContent is not None:
                        structured = json.dumps(
                            remote.structuredContent, ensure_ascii=False
                        ).encode("utf-8")
                        if len(structured) > 32768:
                            preserved = (
                                structured[:1048576]
                                .decode("utf-8", errors="ignore")
                                .encode("utf-8")
                            )
                            full_preserved = len(preserved) == len(structured)
                            suffix = "json" if full_preserved else "preview.txt"
                            path = (
                                context.artifacts_dir
                                / f"{context.operation_id}.mcp-structured.{suffix}"
                            )
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(preserved)
                            artifact = {
                                "artifact_id": str(path),
                                "path": str(path),
                                "sha256": sha256_bytes(preserved),
                                "byte_length": len(preserved),
                            }
                            result["data"]["structured_content"] = {
                                "truncated": True,
                                "artifact": artifact,
                                "original_utf8_bytes": len(structured),
                                "original_sha256": sha256_bytes(structured),
                                "original_preserved": full_preserved,
                            }
                            result["data"]["artifacts"] = [artifact]
                            result["artifact_ids"] = (str(path),)
                    result["data"].update(
                        {
                            "server_id": connection.config.server_id,
                            "original_tool_name": original,
                            "confirmed_read_only": confirmed,
                        }
                    )
                    return result
                except (MCPFailure, TimeoutError, asyncio.CancelledError) as exc:
                    cancelled = isinstance(exc, asyncio.CancelledError)
                    if connection.failed or connection.closed:
                        for name in self._remote_names.get(connection.config.server_id, []):
                            self.registry.unregister(name)
                        for record in self._servers:
                            if record["server_id"] == connection.config.server_id:
                                record["status"] = "unavailable"
                        self._update_context()
                    await self._emit(
                        "mcp.call_failed",
                        {
                            "server_id": connection.config.server_id,
                            "original_tool_name": original,
                            "result_unknown": not confirmed,
                            "cancelled": cancelled,
                            "retryable": False,
                        },
                    )
                    return {
                        "status": "cancelled"
                        if confirmed and cancelled
                        else "error"
                        if confirmed
                        else "unknown",
                        "content": "MCP call did not produce a confirmed result; it will not be replayed automatically.",
                        "data": {
                            "code": "MCP_REQUEST_FAILED" if confirmed else "MCP_RESULT_UNKNOWN",
                            "server_id": connection.config.server_id,
                            "original_tool_name": original,
                            "cancelled": cancelled,
                            "retryable": False,
                        },
                    }

            return PreparedTool(
                {"server_id": connection.config.server_id, "original_tool_name": original}, execute
            )

        self.registry.register(
            visible,
            description[:1024],
            RemoteArguments,
            prepare,
            schema=schema,
            read_only=confirmed,
            remote=True,
        )
        self._registered.append(visible)

    async def aclose(self) -> None:
        failure: BaseException | None = None
        for connection in reversed(self._connections):
            try:
                await self._close_connection(connection)
            except BaseException as exc:
                if failure is None:
                    failure = exc
        self._connections.clear()
        for name in self._registered:
            self.registry.unregister(name)
        self._registered.clear()
        self.registry.extension_context = None
        self.registry.refresh_extensions = None
        if failure is not None:
            raise failure

    async def _close_connection(self, connection: MCPConnection) -> None:
        await connection.close()
        raw = bytes(connection.stderr.raw)
        text = raw.decode("utf-8", errors="replace")
        for name, value in connection.environment.items():
            if value and any(
                word in name.casefold() for word in ("key", "secret", "token", "password", "auth")
            ):
                text = text.replace(value, "[REDACTED]")
        artifact: JSONDict | None = None
        if text:
            safe = text.encode("utf-8")
            path = self.store.artifacts_dir / (
                "mcp-" + sha256_bytes(connection.config.server_id.encode())[:16] + ".stderr.txt"
            )
            path.write_bytes(safe)
            artifact = {
                "path": str(path),
                "sha256": sha256_bytes(safe),
                "byte_length": len(safe),
                "total_bytes": connection.stderr.total_bytes,
                "truncated": connection.stderr.total_bytes > len(raw),
            }
        await self._emit(
            "mcp.closed",
            {"server_id": connection.config.server_id, "stderr_artifact": artifact},
        )

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


__all__ = ["ExtensionManager"]
