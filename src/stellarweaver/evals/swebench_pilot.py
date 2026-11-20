"""Opt-in SWE-bench Lite Marshmallow pilot with an issue-only reproduction."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from stellarweaver.cli import build_provider, effective_config
from stellarweaver.config import AppConfig
from stellarweaver.domain import AgentEvent, JSONDict
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.loop import CompletionProof
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import (
    CommandRunner,
    RunCommandArgs,
    controlled_environment,
    execute_command,
    prepare_run_command,
)
from stellarweaver.tools.registry import Prepare, PreparedTool, ToolContext, ToolRegistry
from stellarweaver.tools.workspace import ToolError

INSTANCE = "marshmallow-code__marshmallow-1359"
REPO = "marshmallow-code/marshmallow"
BASE = "b40a0f4e33823e6d0f341f7e8684e359a99060d1"
IMAGE = "python:3.12-slim-bookworm"
TESTS = ("pytest", "-q", "tests/test_fields.py", "tests/test_schema.py")
REPRO = ("python", "/repro/reproduce.py")
PILOT_COMMANDS = {
    "run_visible_tests": (
        "Run the configured visible repository tests in the isolated container.",
        TESTS,
    ),
    "run_reproducer": ("Run the public issue reproducer in the isolated container.", REPRO),
}
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
        env=controlled_environment({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }),
        timeout=30,
    )


def checkout(source: Path, destination: Path, base_commit: str | None = None) -> None:
    git(source, "clone", "--quiet", "--no-hardlinks", "--no-checkout", ".", str(destination))
    git(destination, "config", "core.autocrlf", "false")
    git(destination, "checkout", "--quiet", "--detach", base_commit or BASE)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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
    if (
        not isinstance(issue, dict)
        or issue.get("instance_id") != INSTANCE
        or not isinstance(issue.get("problem_statement"), str)
        or not issue["problem_statement"].strip()
    ):
        raise ValueError("Unexpected SWE-bench instance")
    source = assets / "marshmallow-1359"
    objects = git(
        source, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
    )
    commits = [entry.split()[0] for entry in objects.splitlines() if entry.endswith(b" commit")]
    if commits != [BASE.encode("ascii")]:
        raise ValueError("The pilot checkout must contain only the pinned base commit")
    dependencies = (assets / "test-deps").resolve(strict=True)
    image = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
        env=controlled_environment(),
        encoding="ascii",
        timeout=15,
    ).strip()
    output.mkdir(parents=True)
    workspace = output / "checkout"
    checkout(source, workspace)
    repro_dir = output / "repro"
    repro_dir.mkdir()
    (repro_dir / "reproduce.py").write_bytes(REPRODUCER)
    return {"problem_statement": issue["problem_statement"]}, PilotRunner(
        workspace, dependencies, repro_dir, image
    )


def register_pilot_tools(registry: ToolRegistry, runner: PilotRunner) -> None:
    register_fixed_tools(registry, runner.run, PILOT_COMMANDS)


def register_fixed_tools(
    registry: ToolRegistry,
    runner: CommandRunner,
    commands: Mapping[str, tuple[str, tuple[str, ...]]],
) -> None:
    def bind(argv: tuple[str, ...]) -> Prepare:
        async def prepare_fixed(_: NoArguments, context: ToolContext) -> PreparedTool:
            return await prepare_run_command(
                RunCommandArgs(argv=list(argv)),
                replace(context, command_runner=runner),
            )

        return prepare_fixed

    registry.unregister("run_command")
    for name, (description, argv) in commands.items():
        registry.register(
            name, description, NoArguments, bind(argv), authorization_tool="run_command"
        )


def export_candidate(assets: Path, output: Path, workspace: Path, model_id: str) -> JSONDict:
    before = snapshot(workspace)
    git(workspace, "add", "-N", "--", ".")
    patch = git(workspace, "diff", "--no-ext-diff", "--binary", BASE, "--")
    after = snapshot(workspace)
    destination = output / "candidate.patch"
    destination.write_bytes(patch)
    evidence = {"sha256": digest(patch), "bytes": len(patch), "snapshot_sha256": after}
    if before != after:
        return {**evidence, "status": "invalid_patch", "reason": "candidate_snapshot_changed"}
    if not patch:
        return {**evidence, "status": "empty_patch", "reason": "no_changes_from_base"}
    with tempfile.TemporaryDirectory(prefix="apply-check-", dir=output) as verification:
        clean = Path(verification) / "checkout"
        checkout(assets / "marshmallow-1359", clean)
        try:
            git(clean, "apply", "--check", str(destination.resolve()))
            text = patch.decode("utf-8")
        except (subprocess.CalledProcessError, UnicodeDecodeError):
            return {**evidence, "status": "invalid_patch", "reason": "patch_not_exportable"}
    prediction = {"instance_id": INSTANCE, "model_name_or_path": model_id, "model_patch": text}
    (output / "predictions.jsonl").write_text(json.dumps(prediction) + "\n", encoding="utf-8")
    return {**evidence, "status": "valid_patch", "reason": "applies_to_base"}


async def run_pilot(
    assets: Path,
    output: Path,
    env_file: Path,
    gate: bool,
    replay_patch: Path | None,
    *,
    replay: bool,
    fake_script: Path | None = None,
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
                {"tool": "run_command", "argv": argv, "cwd": "."}
                for _, argv in PILOT_COMMANDS.values()
            ],
        },
    )
    provider: Literal["fake", "deepseek"] = "fake" if fake_script is not None else "deepseek"
    config = effective_config(AppConfig(), provider, "workspace-edit", True)
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
    model = (
        FakeModel.from_script(fake_script)
        if fake_script is not None
        else build_provider(config, workspace, "deepseek", None, env_file, stream=True)
    )
    prompt = (
        f"Fix {INSTANCE} in {REPO}. The workspace contains base_commit {BASE}. "
        "Use search_text and read_file to locate the problem. Use git_status and git_diff "
        "to inspect the working tree. Use run_visible_tests and run_reproducer with {} "
        "to run the configured container checks. Review the diff and report actual evidence.\n\n"
        + issue["problem_statement"]
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
                completion_check=runner.check if gate else None,
            )
            register_pilot_tools(agent.registry, runner)
            outcome = await agent.run(prompt)
            checks = [item for item in outcome.evidence if item.get("kind") == "completion_check"]
            report = {
                "instance_id": INSTANCE,
                "repo": REPO,
                "base_commit": BASE,
                "session_id": store.session_id,
                "provider": provider,
                "model": config.model.model if provider == "deepseek" else "scripted-reference",
                "thinking": config.model.thinking if provider == "deepseek" else None,
                "reasoning_effort": config.model.reasoning_effort
                if provider == "deepseek"
                else None,
                "outcome": asdict(outcome),
                "tools": [asdict(tool) for tool in agent.results],
                "image": runner.image,
            }
    finally:
        if isinstance(model, OpenAICompatibleChatProvider):
            await model.aclose()
    candidate = await asyncio.to_thread(
        export_candidate, assets, output, workspace, f"stellarweaver-{report['model']}"
    )
    last = checks[-1] if checks else None
    reproduction_status = last["status"] if last else "unavailable" if gate else "not_run"
    reproduction_reason = last["reason"] if last else "no_completed_check"
    if candidate["reason"] == "candidate_snapshot_changed" or (
        last and last["evidence"].get("snapshot_sha256") not in {candidate["snapshot_sha256"], None}
    ):
        reproduction_status, reproduction_reason = "unavailable", "candidate_snapshot_changed"
    report["reproduction"] = {
        "enabled": gate,
        "baseline": asdict(baseline),
        "checks": checks,
        "status": reproduction_status,
        "reason": reproduction_reason,
    }
    report["candidate"] = candidate
    report["official_evaluation"] = {
        "status": "not_evaluated",
        "reason": "pending_independent_evaluation"
        if candidate["status"] == "valid_patch"
        else candidate["status"],
        "resolved": None,
    }
    write_json(output / "agent-report.json", report)
    print(
        outcome.run_status,
        candidate["status"],
        reproduction_status,
        outcome.model_calls,
        outcome.tool_calls,
        flush=True,
    )
    return int(
        outcome.run_status != "completed"
        or candidate["status"] != "valid_patch"
        or (gate and reproduction_status != "passed")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("replay", "agent"))
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="A new output directory")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--patch", type=Path, help="Candidate or reference patch for replay")
    parser.add_argument(
        "--fake-script", type=Path, help="Use an offline FakeModel script in agent mode"
    )
    parser.add_argument(
        "--no-gate", action="store_true", help="Control run with the same allowed commands"
    )
    args = parser.parse_args()
    if args.patch is not None and args.mode != "replay":
        parser.error("--patch is only available with replay")
    if args.fake_script is not None and args.mode != "agent":
        parser.error("--fake-script is only available with agent")
    return asyncio.run(
        run_pilot(
            args.assets.resolve(),
            args.output.resolve(),
            args.env_file.resolve(),
            not args.no_gate,
            args.patch.resolve() if args.patch else None,
            replay=args.mode == "replay",
            fake_script=args.fake_script.resolve() if args.fake_script else None,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
