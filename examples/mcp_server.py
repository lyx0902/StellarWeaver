"""Local stdio MCP fixture: fixed source lookup, paginated discovery and delay tests.

Run via the explicitly configured StellarWeaver extension client. No network,
account, arbitrary file reader or subprocess tool is exposed by this server.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("stellarweaver-local-fixture")
fixture = Path(__file__).parent / "fixtures" / "sample.py"
changed = False


def declaration(name: str) -> types.Tool:
    return types.Tool(
        name=name,
        description="Find a symbol in a fixed local example source file.",
        inputSchema={
            "type": "object",
            "properties": {"symbol": {"type": "string", "minLength": 1}},
            "required": ["symbol"],
            "additionalProperties": False,
        },
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )


@server.list_tools()
async def list_tools(request: types.ListToolsRequest) -> types.ListToolsResult:
    cursor = request.params.cursor if request.params is not None else None
    if cursor is None:
        return types.ListToolsResult(tools=[declaration("lookup_symbol")], nextCursor="page-2")
    if cursor == "page-2":
        return types.ListToolsResult(
            tools=[
                declaration("lookup_second" if changed else "slow_lookup"),
                declaration("unsupported"),
                declaration("refresh_tools"),
                declaration("disconnect"),
                declaration("environment_present"),
            ]
        )
    raise ValueError("Unknown cursor")


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    global changed
    if name == "disconnect":
        os._exit(7)
    if name == "refresh_tools":
        changed = True
        await server.request_context.session.send_tool_list_changed()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="Tool list changed")]
        )
    if name == "environment_present":
        return types.CallToolResult(
            content=[], structuredContent={"present": arguments["symbol"] in os.environ}
        )
    if name == "slow_lookup":
        await asyncio.sleep(3)
    if name == "unsupported":
        return types.CallToolResult(
            content=[types.ImageContent(type="image", data="AA==", mimeType="image/png")]
        )
    symbol = arguments["symbol"]
    matches = [
        {"path": "examples/fixtures/sample.py", "line": index, "text": line.strip()}
        for index, line in enumerate(fixture.read_text(encoding="utf-8").splitlines(), start=1)
        if line.lstrip().startswith((f"def {symbol}(", f"class {symbol}"))
    ]
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=str(matches))],
        structuredContent={"matches": matches},
    )


async def main() -> None:
    # stderr is deliberately separate from the JSON-RPC stdout channel.
    print("local fixture initialized", file=sys.stderr, flush=True)
    if token := os.environ.get("STELLARWEAVER_FIXTURE_LOG_TOKEN"):
        print(token, file=sys.stderr, flush=True)
    if count := os.environ.get("STELLARWEAVER_FIXTURE_STDERR_BYTES"):
        print("x" * min(int(count), 100000), file=sys.stderr, flush=True)
    if path := os.environ.get("STELLARWEAVER_FIXTURE_PID_FILE"):
        await asyncio.to_thread(Path(path).write_text, str(os.getpid()), encoding="utf-8")
    if delay := os.environ.get("STELLARWEAVER_FIXTURE_STARTUP_DELAY"):
        await asyncio.sleep(min(float(delay), 30))
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
