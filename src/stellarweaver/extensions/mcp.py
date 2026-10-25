"""Official MCP 1.26 client sessions over local stdio, with owned lifetimes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, ListToolsResult, ServerNotification, Tool
from pydantic import BaseModel, ConfigDict, Field

from stellarweaver.domain import JSONDict


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    server_id: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1, max_length=4096)
    argv: tuple[str, ...] = ()
    cwd: str = "."
    allow_env: tuple[str, ...] = ()
    read_only_tools: tuple[str, ...] = ()
    startup_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    call_timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class ExtensionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    servers: tuple[ServerConfig, ...] = Field(default=(), max_length=32)


def tool_name(server_id: str, original_name: str) -> str:
    def normalize(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", value) or "unnamed"

    identity = hashlib.sha256((server_id + "\0" + original_name).encode()).hexdigest()[:16]
    return f"mcp__{normalize(server_id)[:16]}__{normalize(original_name)[:22]}_{identity}"


class MCPFailure(Exception):
    pass


class _StderrCapture:
    """Drain a real stderr pipe without storing unbounded server output."""

    def __init__(self, limit: int = 32768) -> None:
        read_fd, write_fd = os.pipe()
        self.writer = os.fdopen(write_fd, "w", encoding="utf-8")
        self.raw = bytearray()
        self.total_bytes = 0

        def drain() -> None:
            with os.fdopen(read_fd, "rb") as stream:
                while chunk := stream.read(4096):
                    self.total_bytes += len(chunk)
                    self.raw.extend(chunk[: max(0, limit - len(self.raw))])

        self.thread = threading.Thread(target=drain, name="stellarweaver-mcp-stderr", daemon=True)
        self.thread.start()

    async def close(self) -> None:
        self.writer.close()
        await asyncio.to_thread(self.thread.join, 2.0)


class MCPConnection:
    """A dedicated task owns SDK cancel scopes from connect through close."""

    def __init__(self, config: ServerConfig, cwd: Path, environment: dict[str, str]) -> None:
        self.config = config
        self.cwd = cwd
        self.environment = environment
        self.protocol_version = ""
        self.stderr = _StderrCapture()
        self._queue: asyncio.Queue[tuple[str, Any, asyncio.Future[Any]] | None] = asyncio.Queue()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task: asyncio.Task[None] | None = None
        self.closed = False
        self.failed = False
        self.tools_changed = False
        self.on_tools_changed: Callable[[], Awaitable[None]] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._serve())
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self.config.startup_timeout_seconds)
        except (TimeoutError, MCPFailure):
            await self.close()
            raise MCPFailure("MCP initialization failed or timed out") from None

    async def _serve(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(
                    stdio_client(
                        StdioServerParameters(
                            command=self.config.command,
                            args=list(self.config.argv),
                            cwd=self.cwd,
                            env=self.environment,
                        ),
                        errlog=self.stderr.writer,
                    )
                )

                async def message_handler(message: Any) -> None:
                    if (
                        isinstance(message, ServerNotification)
                        and message.root.method == "notifications/tools/list_changed"
                    ):
                        self.tools_changed = True

                read_stream, write_stream = streams
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream, message_handler=message_handler)
                )
                initialized = await session.initialize()
                self.protocol_version = str(initialized.protocolVersion)
                self._ready.set_result(None)
                while True:
                    job = await self._queue.get()
                    if job is None:
                        break
                    method, arguments, future = job
                    try:
                        if method == "list":
                            value: Any = await session.list_tools(cursor=arguments)
                        else:
                            name, values = arguments
                            value = await session.call_tool(
                                name,
                                values,
                                read_timeout_seconds=timedelta(
                                    seconds=self.config.call_timeout_seconds
                                ),
                            )
                        if not future.done():
                            future.set_result(value)
                    except Exception:
                        if not future.done():
                            future.set_exception(
                                MCPFailure("MCP request failed, timed out or disconnected")
                            )
                        # A call with unknown effects is never retried or reconnected automatically.
                        self.failed = True
                        break
        except asyncio.CancelledError:
            self.failed = True
        except Exception:
            self.failed = True
        finally:
            if not self._ready.done():
                self._ready.set_exception(MCPFailure("MCP initialization failed"))
            while not self._queue.empty():
                job = self._queue.get_nowait()
                if job is not None and not job[2].done():
                    job[2].set_exception(MCPFailure("MCP connection closed"))
            await self.stderr.close()
            self.closed = True

    async def _request(self, method: str, args: Any, deadline_seconds: float) -> Any:
        if self.closed or self.failed:
            raise MCPFailure("MCP connection unavailable")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self._queue.put((method, args, future))
        try:
            return await asyncio.wait_for(asyncio.shield(future), deadline_seconds)
        except (TimeoutError, asyncio.CancelledError):
            # Stop the session so a timed-out job cannot later appear successful.
            future.cancel()
            await self.close()
            raise

    async def list_tools(self) -> tuple[Tool, ...]:
        tools: list[Tool] = []
        cursor: str | None = None
        seen: set[str] = set()
        async with asyncio.timeout(self.config.startup_timeout_seconds):
            for _ in range(32):
                page: ListToolsResult = await self._request(
                    "list", cursor, self.config.startup_timeout_seconds
                )
                tools.extend(page.tools)
                if len(tools) > 256:
                    raise MCPFailure("MCP discovery exceeds 256 tools")
                cursor = page.nextCursor
                if cursor is None:
                    if len({tool.name for tool in tools}) != len(tools):
                        raise MCPFailure("MCP discovery returned duplicate tool identities")
                    return tuple(tools)
                if cursor in seen:
                    raise MCPFailure("MCP discovery returned a cursor loop")
                seen.add(cursor)
        raise MCPFailure("MCP discovery exceeds 32 pages")

    async def call(self, name: str, args: JSONDict) -> CallToolResult:
        result: CallToolResult = await self._request(
            "call", (name, args), self.config.call_timeout_seconds + 0.5
        )
        return result

    async def close(self) -> None:
        if self._task is None:
            if not self.closed:
                await self.stderr.close()
                self.closed = True
            return
        await self._queue.put(None)
        # Failed workers are already unwinding SDK scopes and draining stderr;
        # cancelling them here can interrupt that cleanup after ready failed.
        if not self._task.done() and not self.failed:
            self._task.cancel()
        await asyncio.wait_for(asyncio.shield(self._task), 10.0)
        if self._ready.done() and not self._ready.cancelled():
            self._ready.exception()


def normalize_result(result: CallToolResult, *, limit: int = 32768) -> JSONDict:
    texts: list[str] = []
    unsupported: list[str] = []
    for block in result.content:
        if block.type == "text":
            texts.append(block.text)
        else:
            unsupported.append(block.type)
    text = "\n".join(texts)
    raw = text.encode("utf-8")
    truncated = len(raw) > limit
    if truncated:
        text = raw[:limit].decode("utf-8", errors="ignore") + "\n[MCP text output truncated]"
    if unsupported:
        text += "\n[Unsupported MCP content types: " + ", ".join(unsupported) + "]"
    structured = result.structuredContent
    if structured is not None:
        encoded = json.dumps(structured, ensure_ascii=False).encode("utf-8")
        if len(encoded) > limit:
            structured = {
                "truncated": True,
                "original_utf8_bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "original_preserved": False,
                "note": "Structured content omitted from this bounded projection; no artifact supplied here.",
            }
    return {
        "status": "error" if result.isError else "ok",
        "content": text,
        "data": {
            "structured_content": structured,
            "unsupported_content_types": unsupported,
            "truncated": truncated,
            "text_total_bytes": len(raw),
            "is_error": result.isError,
        },
    }
