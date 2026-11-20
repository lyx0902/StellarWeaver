"""Opt-in SWE-bench Lite Marshmallow pilot with an issue-only reproduction."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Literal
from uuid import uuid4

from stellarweaver.cli import build_provider, effective_config
from stellarweaver.config import AppConfig
from stellarweaver.domain import AgentEvent, JSONDict
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.loop import CompletionProof
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import controlled_environment, execute_command
from stellarweaver.tools.workspace import ToolError

INSTANCE = "marshmallow-code__marshmallow-1359"
BASE = "b40a0f4e33823e6d0f341f7e8684e359a99060d1"
IMAGE = "python:3.12-slim-bookworm"
TESTS = ("pytest", "-q", "tests/test_fields.py", "tests/test_schema.py")
REPRO = ("python", "/repro/reproduce.py")
EXPECTED_ERROR = "AttributeError: 'List' object has no attribute 'opts'"
REPRODUCER = b"""\
from marshmallow import Schema, fields

class MySchema(Schema):
    times = fields.List(fields.DateTime())

MySchema()
"""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, body: JSONDict) -> None:
    path.write_text(json.dumps(body, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def git(root: Path, *argv: str) -> bytes:
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", f"core.hooksPath={os.devnull}", *argv],
        env=controlled_environment(),
        timeout=30,
    )


def checkout(source: Path, destination: Path) -> None:
    git(source, "clone", "--quiet", "--no-hardlinks", "--no-checkout", ".", str(destination))
    git(destination, "config", "core.autocrlf", "false")
    git(destination, "checkout", "--quiet", "--detach", BASE)


def snapshot(root: Path) -> str:
    entries: dict[str, str] = {}
    total = 0
    for directory, folders, files in os.walk(root):
        current = Path(directory)
        if current == root and ".git" in folders:
            folders.remove(".git")
        for name in (*folders, *files):
            if (current / name).is_symlink():
                raise ValueError("The verification snapshot contains a symlink")
        for name in files:
            path = current / name
            total += path.stat().st_size
            if len(entries) >= 6000 or total > 32 * 1024 * 1024:
                raise ValueError("The verification snapshot exceeds the pilot limits")
            entries[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return digest(json.dumps(entries, sort_keys=True).encode())


def classify(
    result: JSONDict, *, baseline: bool
) -> tuple[Literal["passed", "failed", "unavailable"], str]:
    data = result["data"]
    if data.get("timed_out"):
        return "unavailable", "verification_timeout"
    code = data.get("exit_code")
    if (
        code in {None, 125, 126, 127}
        or data.get("output_incomplete")
        or data.get("cancelled")
        or result["status"] in {"unknown", "cancelled"}
    ):
        return "unavailable", "runner_unavailable"
    stderr = data.get("stderr", "")
    last_line = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    if code == 1 and last_line == EXPECTED_ERROR:
        return (
            ("passed", "baseline_reproduced") if baseline else ("failed", "original_error_remains")
        )
    if "ModuleNotFoundError:" in stderr or "ImportError:" in stderr:
        return "unavailable", "dependency_or_import_error"
    if baseline:
        return "unavailable", "baseline_did_not_reproduce"
    if code == 0 and result["status"] == "ok":
        return "passed", "reproducer_passed"
    return "failed", "candidate_failed_with_different_error"


class PilotRunner:
    def __init__(self, workspace: Path, dependencies: Path, repro_dir: Path, image: str) -> None:
        self.workspace = workspace
        self.dependencies = dependencies
        self.repro_dir = repro_dir
        self.image = image

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        *,
        config: object = None,
        artifacts_dir: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> JSONDict:
        command = tuple(argv)
        if command not in {TESTS, REPRO} or cwd != self.workspace:
            raise ToolError("COMMAND_NOT_CONFIGURED", "Select one of the two fixed pilot commands.")
        name = "stellarweaver-swepilot-" + uuid4().hex[:16]
        docker_argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=768m",
            "--mount",
            f"type=bind,src={self.workspace},dst=/work,readonly",
            "--mount",
            f"type=bind,src={self.dependencies},dst=/deps,readonly",
            "--mount",
            f"type=bind,src={self.repro_dir},dst=/repro,readonly",
            "--workdir=/work",
            "--env=PYTHONPATH=/work/src:/deps",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            self.image,
            "python",
            "-c",
        ]
        if command == REPRO:
            docker_argv += [
                "import runpy, setuptools; runpy.run_path('/repro/reproduce.py', run_name='__main__')",
            ]
        else:
            docker_argv += [
                "import sys, setuptools, pytest; raise SystemExit(pytest.main(sys.argv[1:]))",
                "-p",
                "no:cacheprovider",
                *argv[1:],
            ]
        try:
            result = await execute_command(
                docker_argv,
                cwd,
                config=config,
                artifacts_dir=artifacts_dir,
                timeout_seconds=min(timeout_seconds if timeout_seconds is not None else 90, 120),
            )
        finally:
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "rm", "--force", name],
                timeout=10,
                check=False,
                env=controlled_environment(),
                capture_output=True,
            )
        result["data"].update(argv=argv, cwd=".", backend="linux_docker")
        return result

    async def check(self, seconds: float, *, baseline: bool = False) -> CompletionProof:
        script = self.repro_dir / "reproduce.py"
        evidence: JSONDict = {"reproducer_sha256": digest(REPRODUCER), "argv": list(REPRO)}
        try:
            if script.read_bytes() != REPRODUCER:
                return CompletionProof("unavailable", "reproducer_changed", evidence)
            before = await asyncio.to_thread(snapshot, self.workspace)
            evidence["snapshot_sha256"] = before
            result = await self.run(list(REPRO), self.workspace, timeout_seconds=seconds)
            data = result["data"]
            evidence.update(
                exit_code=data.get("exit_code"),
                diagnostic=str(data.get("stderr", ""))[-2500:],
            )
            if script.read_bytes() != REPRODUCER or before != await asyncio.to_thread(
                snapshot, self.workspace
            ):
                return CompletionProof("unavailable", "verification_inputs_changed", evidence)
            status, reason = classify(result, baseline=baseline)
            return CompletionProof(status, reason, evidence)
        except (OSError, ValueError, ToolError, subprocess.TimeoutExpired) as exc:
            evidence["diagnostic"] = type(exc).__name__
            return CompletionProof("unavailable", "runner_unavailable", evidence)


def prepare(assets: Path, output: Path) -> tuple[JSONDict, PilotRunner]:
    issue = json.loads((assets / "agent-input.json").read_text(encoding="utf-8"))
    if issue["instance_id"] != INSTANCE:
        raise ValueError("Unexpected SWE-bench instance")
    dependencies = (assets / "test-deps").resolve(strict=True)
    image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
        env=controlled_environment(),
        encoding="ascii",
        timeout=15,
    ).strip()
    output.mkdir(parents=True)
    workspace = output / "checkout"
    checkout(assets / "marshmallow-1359", workspace)
    repro_dir = output / "repro"
    repro_dir.mkdir()
    (repro_dir / "reproduce.py").write_bytes(REPRODUCER)
    return issue, PilotRunner(workspace, dependencies, repro_dir, image)


async def run_pilot(
    assets: Path,
    output: Path,
    env_file: Path,
    gate: bool,
    replay_patch: Path | None,
    *,
    replay: bool,
) -> int:
    issue, runner = await asyncio.to_thread(prepare, assets, output)
    workspace = runner.workspace
    baseline = await runner.check(120, baseline=True)
    write_json(output / "baseline.json", asdict(baseline))
    if baseline.status != "passed":
        print(baseline.reason, flush=True)
        return 2
    if replay:
        if replay_patch:
            git(workspace, "apply", "--check", str(replay_patch))
            git(workspace, "apply", str(replay_patch))
        proof = await runner.check(120)
        write_json(
            output / "reproduction.json", {"baseline": asdict(baseline), "candidate": asdict(proof)}
        )
        print(f"{baseline.reason} / {proof.reason}", flush=True)
        return {"passed": 0, "failed": 1, "unavailable": 2}[proof.status]

    policy = output / "policy.json"
    write_json(
        policy,
        {
            "workspace": str(workspace),
            "grants": [
                {"tool": "run_command", "argv": argv, "cwd": "."} for argv in (TESTS, REPRO)
            ],
        },
    )
    config = effective_config(AppConfig(), "deepseek", "workspace-edit", True)
    config = config.model_copy(
        update={
            "limits": config.limits.model_copy(
                update={
                    "max_model_calls": 16,
                    "max_tool_calls": 50,
                    "max_wall_seconds": 360,
                    "max_input_tokens_total": 320_000,
                    "max_output_tokens_total": 16_000,
                }
            )
        }
    )
    model = build_provider(config, workspace, "deepseek", None, env_file, stream=True)
    prompt = (
        "Fix the following issue at the checked-out base revision. The authorized executable "
        f"commands are {json.dumps(TESTS)} for the existing suites and {json.dumps(REPRO)} "
        "for the issue reproduction. Use the exact commands if testing. Review the diff and "
        "report actual evidence.\n\n" + issue["problem_statement"]
    )

    def event(item: AgentEvent) -> None:
        if item.type in {
            "model.started",
            "reproduction.completed",
            "reproduction.feedback",
            "run.completed",
        }:
            suffix = item.payload.get("reason", item.payload.get("request_index", ""))
            print(item.type, suffix, flush=True)

    try:
        with SessionStore(workspace, data_dir=output / "state") as store:
            store.set_config(config.model_dump())
            agent = build_agent(
                config,
                model,
                store,
                policy_file=policy,
                on_event=event,
                command_runner=runner.run,
                completion_check=runner.check if gate else None,
            )
            outcome = await agent.run(prompt)
            checks = [item for item in outcome.evidence if item.get("kind") == "completion_check"]
            report = {
                "instance_id": INSTANCE,
                "base_commit": BASE,
                "session_id": store.session_id,
                "model": config.model.model,
                "thinking": config.model.thinking,
                "reasoning_effort": config.model.reasoning_effort,
                "outcome": asdict(outcome),
                "tools": [asdict(tool) for tool in agent.results],
                "image": runner.image,
            }
    finally:
        if isinstance(model, OpenAICompatibleChatProvider):
            await model.aclose()
    before = await asyncio.to_thread(snapshot, workspace)
    git(workspace, "add", "-N", "--", ".")
    patch = git(workspace, "diff", "--no-ext-diff", "--binary", "HEAD", "--")
    after = await asyncio.to_thread(snapshot, workspace)
    last = checks[-1] if checks else None
    reproduction_status = last["status"] if last else "unavailable" if gate else "not_run"
    reproduction_reason = last["reason"] if last else "no_completed_check"
    if before != after or (last and last["evidence"].get("snapshot_sha256") not in {after, None}):
        reproduction_status, reproduction_reason = "unavailable", "candidate_snapshot_changed"
    report["reproduction"] = {
        "enabled": gate,
        "baseline": asdict(baseline),
        "checks": checks,
        "status": reproduction_status,
        "reason": reproduction_reason,
    }
    (output / "candidate.patch").write_bytes(patch)
    prediction = {
        "instance_id": INSTANCE,
        "model_name_or_path": "stellarweaver-deepseek-flash",
        "model_patch": patch.decode("utf-8"),
    }
    (output / "predictions.jsonl").write_text(json.dumps(prediction) + "\n", encoding="utf-8")
    write_json(output / "agent-report.json", report)
    print(
        outcome.run_status, reproduction_status, outcome.model_calls, outcome.tool_calls, flush=True
    )
    return int(outcome.run_status != "completed" or (gate and reproduction_status != "passed"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("replay", "agent"))
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="A new output directory")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--patch", type=Path, help="Candidate or reference patch for replay")
    parser.add_argument(
        "--no-gate", action="store_true", help="Control run with the same allowed commands"
    )
    args = parser.parse_args()
    if args.patch is not None and args.mode != "replay":
        parser.error("--patch is only available with replay")
    return asyncio.run(
        run_pilot(
            args.assets.resolve(),
            args.output.resolve(),
            args.env_file.resolve(),
            not args.no_gate,
            args.patch.resolve() if args.patch else None,
            replay=args.mode == "replay",
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
