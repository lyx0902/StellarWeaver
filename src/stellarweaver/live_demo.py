"""Explicit, bounded real-provider coding smoke test in a fresh temporary repository.

No API request occurs on import. Run this module deliberately after configuring
an API key. The agent sees only fixture sources; its policy permits one source
file and exact unittest argv. An outer oracle remains outside its workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from stellarweaver.config import ExecutionConfig, LimitsConfig, get_api_key, load_config
from stellarweaver.domain import AgentEvent
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import controlled_environment
from stellarweaver.tools.registry import ToolRegistry

SOURCE = """def parse_page_size(value, default=20):
    try:
        size = int(value)
    except (TypeError, ValueError):
        return default
    return min(size, 100)
"""

VISIBLE_TESTS = """import unittest
from pagination import parse_page_size

class TestPagination(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_page_size("1"), 1)
        self.assertEqual(parse_page_size("100"), 100)

    def test_invalid(self):
        for value in ("0", "-5", "101", "", "abc"):
            with self.subTest(value=value):
                self.assertEqual(parse_page_size(value), 20)
"""

ORACLE = """from pagination import parse_page_size
for value, expected in [
    ("1", 1), ("17", 17), ("100", 100), (" 42 ", 42),
    ("0", 20), ("-1", 20), ("101", 20), ("999", 20),
    ("", 20), ("abc", 20), ("1.5", 20), (None, 20)
]:
    actual = parse_page_size(value)
    assert actual == expected, (value, expected, actual)
assert parse_page_size("0", default=7) == 7
assert parse_page_size("101", default=7) == 7
print("Independent pagination oracle: 14 checks passed")
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_process(argv: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    environment = controlled_environment()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONPATH": str(workspace),
        }
    )
    return subprocess.run(
        argv,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


async def run_live(env_file: Path, output: Path | None, *, streaming: bool = True) -> int:
    launcher = Path.cwd()
    settings = load_config(launcher)
    config = settings.model.model_copy(
        update={
            "provider": "deepseek",
            "supports_streaming": streaming,
            "max_output_tokens": 2048,
        }
    )
    key = get_api_key(config, launcher, env_file)
    if not key:
        raise ValueError(f"Fill {env_file} with {config.api_key_env}")
    directory = Path(tempfile.mkdtemp(prefix="stellarweaver-live-"))
    workspace = directory / "repo"
    workspace.mkdir()
    (workspace / "pagination.py").write_bytes(SOURCE.encode("utf-8"))
    (workspace / "test_pagination.py").write_bytes(VISIBLE_TESTS.encode("utf-8"))
    (workspace / "unrelated.txt").write_bytes(b"Preserve existing fixture content\r\n")
    oracle_path = directory / "oracle.py"
    oracle_path.write_bytes(ORACLE.encode("utf-8"))
    for argv in (
        ["git", "init", "-q"],
        ["git", "add", "--", "pagination.py", "test_pagination.py", "unrelated.txt"],
    ):
        git = await asyncio.to_thread(run_process, argv, workspace)
        if git.returncode:
            raise RuntimeError("Cannot initialize temporary Git fixture")
    before = {
        name: digest(workspace / name)
        for name in ("pagination.py", "test_pagination.py", "unrelated.txt")
    }
    argv = [sys.executable, "-m", "unittest", "-q"]
    baseline = await asyncio.to_thread(run_process, argv, workspace)
    baseline_oracle = await asyncio.to_thread(
        run_process, [sys.executable, str(oracle_path)], workspace
    )
    if baseline.returncode == 0 or baseline_oracle.returncode == 0:
        raise RuntimeError("The fixture did not reproduce its intended defect")
    policy_path = directory / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "grants": [
                    {"tool": "edit_file", "paths": ["pagination.py"]},
                    {"tool": "run_command", "argv": argv, "cwd": "."},
                    {"tool": "run_command", "argv": ["python", "-m", "unittest", "-q"], "cwd": "."},
                ],
            }
        ),
        encoding="utf-8",
    )
    events: list[dict[str, object]] = []

    def observe(event: AgentEvent) -> None:
        events.append(asdict(event))
        if event.type in {"tool.completed", "model.retry_scheduled"}:
            print(
                json.dumps(
                    {
                        "type": event.type,
                        "tool": event.payload.get("tool_name"),
                        "status": event.payload.get("status"),
                    }
                ),
                flush=True,
            )

    provider = OpenAICompatibleChatProvider(config, key)
    try:
        with SessionStore(workspace, directory / "evidence") as store:
            registry = ToolRegistry(
                workspace,
                PolicyEngine(workspace=workspace, policy_file=policy_path),
                ExecutionConfig(),
                artifacts_dir=store.artifacts_dir,
            )
            agent = Agent(
                provider,
                registry,
                store=store,
                on_event=observe,
                limits=LimitsConfig(
                    max_model_calls=12,
                    max_tool_calls=40,
                    max_wall_seconds=180,
                    max_input_tokens_total=200000,
                    max_output_tokens_total=12000,
                ),
                max_output_tokens=2048,
                stream=streaming,
            )
            prompt = (
                "Repair parse_page_size in pagination.py. Valid integer inputs in range "
                "1..100 return that integer. Zero, negatives, values above 100 and "
                "non-integer inputs must return the supplied default (20 if omitted). "
                "Inspect the implementation and tests using file tools. First run the "
                "existing tests to observe failures, then edit ONLY pagination.py using "
                "read_file SHA-256 and edit_file. Preserve test_pagination.py and unrelated.txt. "
                "Do not create files. Run the same tests again after the fix and report real "
                "results. The exact authorized run_command argv is "
                + json.dumps(argv)
                + ', cwd is ".". Do not use shell wrappers or other command arguments.'
            )
            outcome = await agent.run(prompt)
            final_oracle = await asyncio.to_thread(
                run_process, [sys.executable, str(oracle_path)], workspace
            )
            after = {name: digest(workspace / name) for name in before}
            preserved = all(before[n] == after[n] for n in ("test_pagination.py", "unrelated.txt"))
            calls = sum(len(m.tool_calls) for m in agent.messages if m.role == "assistant")
            results = sum(m.role == "tool" for m in agent.messages)
            command_codes = [
                r.data["exit_code"]
                for r in agent.results
                if r.tool_name == "run_command" and "exit_code" in r.data
            ]
            report = {
                "checked_at": datetime.now(UTC).isoformat(),
                "provider": "deepseek",
                "model": config.model,
                "streaming": streaming,
                "workspace": str(workspace),
                "baseline_test_exit_code": baseline.returncode,
                "baseline_oracle_exit_code": baseline_oracle.returncode,
                "agent_command_exit_codes": command_codes,
                "oracle_exit_code": final_oracle.returncode,
                "oracle_stdout": final_oracle.stdout,
                "oracle_stderr": final_oracle.stderr,
                "protected_fixture_preserved": preserved,
                "assistant_tool_calls": calls,
                "tool_results": results,
                "before_sha256": before,
                "after_sha256": after,
                "outcome": asdict(outcome),
                "success": (
                    outcome.run_status == "completed"
                    and outcome.verification_status == "passed"
                    and final_oracle.returncode == 0
                    and preserved
                    and calls == results
                    and before["pagination.py"] != after["pagination.py"]
                    and len(command_codes) >= 2
                    and command_codes[0] != 0
                    and command_codes[-1] == 0
                ),
            }
            destination = output or directory / "live_report.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Redact the configured key even if a vendor echoes it unexpectedly.
            serialized = json.dumps(report, ensure_ascii=True, indent=2).replace(key, "[REDACTED]")
            destination.write_text(serialized + "\n", encoding="utf-8")
            (destination.parent / (destination.stem + "_events.json")).write_text(
                json.dumps(events, ensure_ascii=True, indent=2).replace(key, "[REDACTED]"),
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "success": report["success"],
                        "report": str(destination),
                        "workspace": str(workspace),
                        "model_calls": outcome.model_calls,
                        "tool_calls": outcome.tool_calls,
                        "run_status": outcome.run_status,
                        "command_exit_codes": command_codes,
                        "oracle_exit_code": final_oracle.returncode,
                        "protected_fixture_preserved": preserved,
                    }
                ),
                flush=True,
            )
            return 0 if report["success"] else 1
    finally:
        await provider.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(run_live(args.env_file, args.output, streaming=not args.no_stream))
    )


if __name__ == "__main__":
    main()
