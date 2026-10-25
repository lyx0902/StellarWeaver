"""One-command offline skills/MCP demonstration with temporary exact grants."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from stellarweaver.demo import find_demo_git, initialize_demo_repository
from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.extensions import ExtensionManager
from stellarweaver.extensions.mcp import tool_name
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


async def demonstrate(root: Path, state: Path, server: Path) -> dict[str, object]:
    skill = root / ".stellarweaver/skills/find-symbol/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: find-symbol\ndescription: Locate the fixed add fixture symbol\n---\n"
        "Use the local lookup_symbol MCP tool with symbol add. Report its actual path and line.\n",
        encoding="utf-8",
    )
    argv = [sys.executable, str(server)]
    config = state / "extensions.json"
    config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "server_id": "fixture",
                        "command": argv[0],
                        "argv": argv[1:],
                        "cwd": ".",
                        "read_only_tools": ["lookup_symbol"],
                    }
                ]
            }
        )
    )
    grants = state / "policy.json"
    grants.write_text(
        json.dumps(
            {
                "workspace": str(root),
                "grants": [
                    {
                        "tool": "mcp_start",
                        "server_id": "fixture",
                        "argv": argv,
                        "cwd": ".",
                    }
                ],
            }
        )
    )
    stage = 0
    location: dict[str, object] = {}

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal stage, location
        observed = [json.loads(item.content) for item in request.messages if item.role == "tool"]
        if stage == 0:
            stage += 1
            return AssistantReply(
                tool_calls=(ToolCall("skill", "load_skill", '{"name":"find-symbol"}'),),
                stop_reason="tool_calls",
            )
        if stage == 1:
            assert observed[-1]["status"] == "ok" and "symbol add" in observed[-1]["content"]
            stage += 1
            return AssistantReply(
                tool_calls=(
                    ToolCall("lookup", tool_name("fixture", "lookup_symbol"), '{"symbol":"add"}'),
                ),
                stop_reason="tool_calls",
            )
        location = observed[-1]["data"]["structured_content"]["matches"][0]
        return AssistantReply(f"Observed {location['path']}:{location['line']}")

    with SessionStore(root, state / "sessions") as store:
        registry = ToolRegistry(
            root,
            PolicyEngine("read-only", workspace=root, policy_file=grants),
            artifacts_dir=store.artifacts_dir,
        )
        async with ExtensionManager(registry, store, config, trusted=True) as extensions:
            if not extensions._connections:
                raise RuntimeError("Local MCP fixture did not connect")
            outcome = await Agent(FakeModel(script), registry, store=store).run(
                "Load find-symbol and apply it to the local fixture"
            )
        events = store.load_events()
        result: dict[str, object] = {
            "run_status": outcome.run_status,
            "model_provider": "scripted FakeModel (offline)",
            "model_calls": outcome.model_calls,
            "tool_calls": outcome.tool_calls,
            "skills_loaded": sum(event.type == "skill.loaded" for event in events),
            "mcp_connections": sum(event.type == "mcp.connected" for event in events),
            "mcp_closed": sum(event.type == "mcp.closed" for event in events),
            "observed_location": location,
        }
        if outcome.run_status != "completed":
            raise RuntimeError(outcome.reason)
        return result


def main() -> None:
    executable = find_demo_git()
    if executable is None:
        raise SystemExit(2)
    server = Path(__file__).parents[3] / "examples/mcp_server.py"
    if not server.is_file():
        raise SystemExit("Run this source-checkout demonstration from the StellarWeaver repository")
    with tempfile.TemporaryDirectory(prefix="stellarweaver-extensions-") as directory:
        base = Path(directory)
        root, state = base / "workspace", base / "state"
        root.mkdir()
        state.mkdir()
        if not initialize_demo_repository(root, executable):
            raise SystemExit(2)
        result = asyncio.run(demonstrate(root, state, server))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
