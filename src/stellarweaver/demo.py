"""Run a scripted model against a newly-created, real temporary Git repository."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from stellarweaver.config import ExecutionConfig
from stellarweaver.domain import AgentEvent, AssistantReply, ModelRequest, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def action(name: str, arguments: dict[str, object]) -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall("demo-call", name, json.dumps(arguments)),),
        stop_reason="tool_calls",
    )


def find_demo_git() -> str | None:
    """Check the calling terminal's prerequisite before creating a workspace."""
    executable = shutil.which("git")
    if executable is None:
        print(
            "Demo requires Git on PATH. Install Git or add its cmd directory to this "
            "terminal's PATH, then verify with `git --version`. "
            "`uv run --offline` avoids network access; it does not bundle Git.",
            file=sys.stderr,
        )
    return executable


def initialize_demo_repository(workspace: Path, executable: str) -> bool:
    try:
        subprocess.run([executable, "init", "-q", str(workspace)], check=True, capture_output=True)
    except OSError as exc:
        print(
            f"Could not start Git: {exc}. Check `git --version` in this terminal.", file=sys.stderr
        )
        return False
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
        print(
            f"Git could not initialize the demonstration repository: {detail.strip()}",
            file=sys.stderr,
        )
        return False
    return True


async def demo() -> int:
    executable = find_demo_git()
    if executable is None:
        return 2
    directory = Path(tempfile.mkdtemp(prefix="stellarweaver-demo-"))
    workspace = directory / "repo"
    workspace.mkdir()
    if not await asyncio.to_thread(initialize_demo_repository, workspace, executable):
        return 2
    source = "def add(a, b):\n    return a - b\n"
    (workspace / "calc.py").write_text(source, encoding="utf-8")
    (workspace / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    (workspace / "keep.txt").write_bytes(b"Existing user content\r\n")
    argv = [sys.executable, "-m", "unittest", "-q"]
    policy_file = directory / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "workspace": str(workspace.resolve()),
                "grants": [
                    {"tool": "edit_file", "paths": ["calc.py"]},
                    {"tool": "run_command", "argv": argv, "cwd": "."},
                ],
            }
        ),
        encoding="utf-8",
    )
    edits = 0
    exit_codes: list[int] = []

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal edits
        observations = [json.loads(m.content) for m in request.messages if m.role == "tool"]
        if not observations:
            return action("git_status", {})
        latest = observations[-1]
        if latest["tool_name"] == "git_status":
            return action("list_files", {})
        if latest["tool_name"] == "list_files":
            return action("search_text", {"pattern": "return", "glob": "*.py"})
        if latest["tool_name"] == "search_text":
            return action("read_file", {"path": "calc.py"})
        if latest["tool_name"] == "read_file":
            edits += 1
            return action(
                "edit_file",
                {
                    "path": "calc.py",
                    "old_text": "return a - b" if edits == 1 else "return a + b + 1",
                    "new_text": "return a + b + 1" if edits == 1 else "return a + b",
                    "expected_sha256": latest["data"]["sha256"],
                },
            )
        if latest["tool_name"] == "edit_file":
            return action("run_command", {"argv": argv})
        if latest["tool_name"] == "run_command":
            exit_codes.append(latest["data"]["exit_code"])
            if latest["data"]["exit_code"] != 0:
                return action("read_file", {"path": "calc.py"})
            return action("git_diff", {})
        return AssistantReply(
            text="The scripted repair observed a failed test, repaired it and observed a pass."
        )

    def show(event: AgentEvent) -> None:
        if event.type == "tool.completed":
            data = event.payload
            print(f"{data['tool_name']}: {data['status']}", flush=True)
        elif event.type.startswith("run."):
            print(event.type, flush=True)

    with SessionStore(workspace, directory / "evidence") as store:
        registry = ToolRegistry(
            workspace,
            PolicyEngine(workspace=workspace, policy_file=policy_file),
            ExecutionConfig(),
            artifacts_dir=store.artifacts_dir,
        )
        agent = Agent(FakeModel(script), registry, store=store, on_event=show)
        result = await agent.run(
            "Find and repair add(), run the actual tests, and fix any failure."
        )
        unexpected = [
            {"tool_name": item.tool_name, "status": item.status, "code": item.data.get("code")}
            for item in agent.results
            if item.tool_name != "run_command" and item.status != "ok"
        ]
        passed = result.run_status == "completed" and exit_codes == [1, 0] and not unexpected
        if (workspace / "keep.txt").read_bytes() != b"Existing user content\r\n":
            raise RuntimeError("Unrelated file changed")
        print(
            json.dumps(
                {
                    "workspace": str(workspace),
                    "demo_passed": passed,
                    "unexpected_tool_failures": unexpected,
                    "test_exit_codes": exit_codes,
                    "outcome": asdict(result),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(demo()))
