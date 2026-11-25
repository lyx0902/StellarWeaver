"""Prepare, solve and independently score a fixed SWE-bench Lite selection."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from stellarweaver.cli import build_provider, effective_config
from stellarweaver.config import (
    AppConfig,
    ContextConfig,
    ExecutionConfig,
    HarnessConfig,
    LimitsConfig,
    ModelConfig,
)
from stellarweaver.domain import DEFAULT_CONTEXT_WINDOW_TOKENS, JSONDict
from stellarweaver.evals.provenance import canonical_json
from stellarweaver.evals.swebench_official import GRADER_VERSION
from stellarweaver.evals.swebench_pilot import (
    checkout,
    digest,
    git,
    register_fixed_tools,
)
from stellarweaver.policy import DEVELOPMENT_TOOLS
from stellarweaver.policy.risk import HARD_PATH_ERRORS, POLICY_VERSION
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.locking import WriterLock
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import CommandRunner, controlled_environment, execute_command
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web.isolation import DockerRunner, Isolation

DATASET_REVISION = "b0dde1093fe417d83b7184254edf8199c1f0dff5"
UPSTREAM_SHA256 = {
    "dev": "b90bcbfaca1b5f65155500124a977876c264a4003ab384aca4dfc39a54bef89f",
    "test": "438e281d80587aa7be470896ce410557002fde02d2ceee3e099331d308f62dd3",
}
SPLIT_COUNT = {"dev": 23, "test": 300}
OFFICIAL = Path(__file__).with_name("swebench_official.py")
ID_PATTERN = r"^[a-z0-9][a-z0-9_.-]*__[a-z0-9][a-z0-9_.-]*-[0-9]+$"
SHA_PATTERN = r"^[a-f0-9]{64}$"
IMAGE_PATTERN = r"^[a-z0-9/._:-]+@sha256:[a-f0-9]{64}$"
GRADER_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "version",
    "environment_setup_commit",
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "eval_type",
    "eval_script",
    "log_parser",
    "image",
)
OFFICIAL_REQUIRED = {
    "instance_id",
    "repo",
    "base_commit",
    "version",
    "eval_script",
    "log_parser",
    "eval_type",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "test_patch",
}
LEGACY_CONTEXT_WINDOW_TOKENS = 100_000
LIMITS = {
    "max_tool_calls": 50,
    "max_wall_seconds": 360,
    "max_input_tokens_total": 320_000,
    "max_output_tokens_total": 16_000,
}
_PIPELINE_OPTIONS = (
    "pipeline_metrics",
    "multi_edit_tools",
    "post_edit_syntax_hook",
    "smart_tool_truncation",
    "navigation_prefilter",
    "multi_file_apply",
    "parallel_read_tools",
    "scoped_test_grants",
    "incremental_sandbox_snapshots",
    "import_aware_repomap",
    "subagent_exploration",
    "calibrated_context_budget",
    "sticky_context_checkpoint",
    "protect_fresh_tool_output",
    "soft_test_failure_reminder",
)
_HARNESS_SWITCHES = (
    "development_tools",
    "issue_navigation",
    "patch_staging",
    "task_evidence",
    "budget_guidance",
    *_PIPELINE_OPTIONS,
)
_EXPLORATION_OPTIONS = (
    "exploration_max_model_calls",
    "exploration_max_tool_calls",
    "exploration_timeout_seconds",
    "exploration_stall_rounds",
)
VISIBLE_PYTEST = """\
import os
import runpy
import shutil
import site
import sys

work = '/tmp/checkout'
shutil.copytree('/work', work, symlinks=True, ignore=shutil.ignore_patterns('.git'))
os.chdir(work)
sys.path[:0] = [work + '/src', work]
site.addsitedir('/deps')
runpy.run_module('pytest', run_name='__main__')
"""
SANDBOX_PYTEST = """\
import os, pathlib, site, sys
if os.path.isdir("/deps"):
    site.addsitedir("/deps")
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
import pytest
root = pathlib.Path.cwd()
sys.path[:0] = [str(root / 'src'), str(root)]
raise SystemExit(pytest.main(sys.argv[1:]))
"""


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(pattern=ID_PATTERN)
    repo: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
    base_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    problem_statement: str = Field(min_length=1)


class Recipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str = Field(pattern=SHA_PATTERN.replace("^", "^sha256:"))
    # Command arguments are operator configuration. The model can only call the fixed tool.
    pytest_args: list[str] = Field(min_length=1)
    dependencies: str | None = None
    grader_image: str | None = Field(default=None, pattern=IMAGE_PATTERN)


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(pattern=ID_PATTERN)
    model_name_or_path: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}$")
    model_patch: str = Field(min_length=1, max_length=8 * 1024 * 1024)


@dataclass(frozen=True)
class RunConfiguration:
    profile: Literal["baseline", "full"]
    sandbox_commands: bool
    adaptive_round_retention: bool
    app: AppConfig

    @classmethod
    def from_manifest(cls, args: argparse.Namespace, manifest: JSONDict) -> RunConfiguration:
        def explicit(name: str, default: Any) -> Any:
            value = getattr(args, name, None)
            return default if value is None else value

        full = getattr(args, "harness_profile", "baseline") == "full"
        sandbox = explicit("sandbox_commands", full)
        harness = HarnessConfig.model_validate(
            {
                **{name: explicit(name, full) for name in _HARNESS_SWITCHES},
                "include_linter": explicit("include_linter", False),
                "repair_controller": explicit("repair_controller", "feedback" if full else "off"),
                "max_length_continuations": 3 if full else 0,
                "repeated_failure_soft_threshold": 3,
                "repeated_failure_hard_threshold": 6 if full else 3,
                "benchmark_allow_denied_tool_continue": full,
                "stream_repetition_detection": full,
                **{
                    name: getattr(args, name)
                    for name in _EXPLORATION_OPTIONS
                    if getattr(args, name, None) is not None
                },
            }
        )
        if harness.development_tools and not sandbox:
            raise ValueError("Development tools require --sandbox-commands")
        app = effective_config(
            AppConfig(
                model=ModelConfig(
                    context_window_tokens=frozen_context_window(manifest),
                    model=manifest.get("model", "deepseek-flash"),
                    thinking=manifest.get("thinking", "enabled"),
                    reasoning_effort=manifest.get("reasoning_effort", "high"),
                ),
                limits=LimitsConfig.model_validate(manifest["limits"]),
                context=ContextConfig(fresh_tool_rounds=explicit("fresh_tool_rounds", 2)),
                harness=harness,
            ),
            "deepseek" if args.live else "fake",
            "benchmark",
            True,
        )
        return cls(
            "full" if full else "baseline", sandbox, explicit("adaptive_round_retention", full), app
        )

    def settings(self) -> JSONDict:
        return {
            "harness_profile": self.profile,
            "sandbox_commands": self.sandbox_commands,
            "adaptive_round_retention": self.adaptive_round_retention,
            "enforce_exploration_caps": True,
            "provider": self.app.model.provider,
            "policy": {"preset": self.app.policy.preset, "version": POLICY_VERSION},
            "limits": self.app.limits.model_dump(),
            "context_window_tokens": self.app.model.context_window_tokens,
            "fresh_tool_rounds": self.app.context.fresh_tool_rounds,
            **self.app.harness.model_dump(),
            "app_config": self.app.model_dump(mode="json"),
        }


def frozen_context_window(manifest: JSONDict) -> int:
    value = manifest.get("context_window_tokens", LEGACY_CONTEXT_WINDOW_TOKENS)
    return TypeAdapter(int).validate_python(value, strict=True)


def run_config_sha256(settings: JSONDict) -> str:
    return digest(
        canonical_json(
            {
                name: value
                for name, value in settings.items()
                if name not in {"config_sha256", "assets", "fixtures", "code_sha", "source_sha256"}
            }
        )
    )


def execution_fingerprint() -> JSONDict:
    package = Path(__file__).resolve().parents[1]
    root = package.parents[1]
    revision = git(root, "rev-parse", "HEAD").decode().strip() if (root / ".git").exists() else None
    return {
        "code_sha": revision,
        "source_sha256": {
            path.relative_to(package).as_posix(): digest(path.read_bytes())
            for path in sorted(package.rglob("*.py"))
        },
    }


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, data: Any, *, jsonl: bool = False) -> None:
    pending = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    body = (
        "".join(json.dumps(row, ensure_ascii=True) + "\n" for row in data)
        if jsonl
        else json.dumps(data, indent=2, ensure_ascii=True) + "\n"
    )
    try:
        pending.write_text(body, encoding="utf-8", newline="\n")
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


@contextmanager
def locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock = WriterLock(root / ".experiment.lock")
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def prepared_tasks(root: Path) -> list[str]:
    ids = read_json(root / "manifest.json")["instance_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(key, str) or not re.fullmatch(ID_PATTERN, key) for key in ids)
        or len(ids) != len(set(ids))
    ):
        raise ValueError("Invalid task selection in experiment manifest")
    return ids


def dataset_selection(args: argparse.Namespace) -> list[JSONDict]:
    if not re.fullmatch(SHA_PATTERN, args.dataset_sha256):
        raise ValueError("A lowercase SHA-256 dataset digest is required")
    if digest(args.dataset.read_bytes()) != args.dataset_sha256:
        raise ValueError("Dataset SHA-256 does not match the supplied checksum")
    if args.legacy_dev and (args.split != "dev" or args.dataset.suffix != ".json"):
        raise ValueError("Legacy replay only accepts a local dev JSON snapshot")
    if not args.legacy_dev and args.dataset.suffix != ".parquet":
        raise ValueError("Use the pinned upstream Parquet file or explicitly opt into legacy dev")
    if not args.legacy_dev and args.dataset_sha256 != UPSTREAM_SHA256[args.split]:
        raise ValueError(f"Parquet does not match official revision {DATASET_REVISION} and split")
    output = subprocess.check_output(
        [str(args.grader_python), "-I", str(OFFICIAL), "dataset", str(args.dataset)],
        env=controlled_environment(),
        timeout=120,
    )
    if digest(args.dataset.read_bytes()) != args.dataset_sha256:
        raise ValueError("Dataset changed during decoding")
    rows = TypeAdapter(list[dict[str, Any]]).validate_json(output)
    if len(rows) != SPLIT_COUNT[args.split]:
        raise ValueError("Dataset row count does not match the requested split")
    index = {row["instance_id"]: row for row in rows}
    if len(index) != len(rows):
        raise ValueError("Duplicate instance IDs")
    requested = args.instance_id or list(index)
    if len(set(requested)) != len(requested) or set(requested) - index.keys():
        raise ValueError("Duplicate or unknown requested instance IDs")
    return [index[key] for key in requested[: args.limit]]


def prepare(args: argparse.Namespace) -> None:
    root = args.output
    if (root / "manifest.json").exists():
        raise ValueError("Existing experiment selection is immutable")
    recipes = (
        TypeAdapter(dict[str, Recipe]).validate_json(args.recipes.read_bytes())
        if args.recipes
        else {}
    )
    limits = LimitsConfig.model_validate(
        {
            **{name: getattr(args, name, default) for name, default in LIMITS.items()},
            "max_model_calls": args.max_model_calls,
        }
    )
    model = ModelConfig(
        provider="deepseek",
        reasoning_effort=getattr(args, "reasoning_effort", "high"),
        context_window_tokens=getattr(args, "context_window_tokens", DEFAULT_CONTEXT_WINDOW_TOKENS),
    )
    manifest: JSONDict = {
        "schema_version": 1,
        "dataset": "SWE-bench/SWE-bench_Lite",
        "dataset_revision": None if args.legacy_dev else DATASET_REVISION,
        "dataset_revision_verified": not args.legacy_dev,
        "dataset_sha256": args.dataset_sha256,
        "split": args.split,
        "limits": limits.model_dump(),
        "context_window_tokens": model.context_window_tokens,
        "model": model.model,
        "thinking": model.thinking,
        "reasoning_effort": model.reasoning_effort,
        "hints": False,
        "web_search": False,
        "completion_gate": False,
        "official_grader_version": GRADER_VERSION,
        "prepared_at": datetime.now(UTC).isoformat(),
        "instance_ids": [],
        "inputs": {},
        **execution_fingerprint(),
    }
    selected = dataset_selection(args)
    if not selected:
        raise ValueError("Select at least one task")
    supplemental = getattr(args, "official_cases", None)
    if supplemental and not args.legacy_dev:
        raise ValueError("Official case supplements are only supported for legacy dev snapshots")
    supplements: dict[str, JSONDict] = {}
    if supplemental:
        for supplement in TypeAdapter(list[dict[str, Any]]).validate_json(
            supplemental.read_bytes()
        ):
            key = supplement["instance_id"]
            if key in supplements or not OFFICIAL_REQUIRED <= supplement.keys():
                raise ValueError("Official case supplement is incomplete or duplicated")
            supplements[key] = supplement
        manifest["official_supplement_sha256"] = digest(supplemental.read_bytes())
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=root) as staging:
        staged = Path(staging) / "cases"
        staged.mkdir()
        for row in selected:
            task = Task.model_validate({name: row[name] for name in Task.model_fields})
            key = task.instance_id
            case = staged / key
            case.mkdir()
            manifest["instance_ids"].append(key)
            save(case / "task.json", task.model_dump())
            scorer = supplements.get(key, row)
            if any(scorer[field] != row[field] for field in ("repo", "base_commit", "version")):
                raise ValueError(f"Official supplement does not match the pinned task {key}")
            save(
                case / "scorer.json",
                {name: scorer[name] for name in GRADER_FIELDS if name in scorer},
            )
            if recipe := recipes.get(key, recipes.get(task.repo)):
                save(case / "recipe.json", recipe.model_dump())
            manifest["inputs"][key] = {
                file.name: digest(file.read_bytes()) for file in case.iterdir()
            }
        # The manifest is committed last. A half-prepared directory cannot be solved.
        cases = root / "cases"
        if cases.exists():
            raise ValueError("Incomplete preparation exists. Use a new experiment directory.")
        staged.rename(cases)
        save(root / "manifest.json", manifest)


def local_image(image: str) -> None:
    subprocess.run(
        ["docker", "image", "inspect", image],
        env=controlled_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=20,
    )


def verify_inputs(root: Path, case: Path, *names: str) -> None:
    expected = read_json(root / "manifest.json")["inputs"][case.name]
    for name in names:
        path = case / name
        if name not in expected or digest(path.read_bytes()) != expected[name]:
            raise ValueError(f"Frozen input {name} changed or is missing for {case.name}")


def git_base(task: Task, assets: Path, workspace: Path) -> None:
    source = assets / task.instance_id
    if source.is_dir():
        checkout(source, workspace, task.base_commit)
    else:
        workspace.mkdir()
        git(workspace, "init", "--quiet")
        git(workspace, "config", "core.autocrlf", "false")
        url = f"https://github.com/{task.repo}.git"
        git(
            workspace,
            "-c",
            "credential.helper=",
            "-c",
            "http.followRedirects=false",
            "fetch",
            "--depth=1",
            "--no-tags",
            url,
            task.base_commit,
        )
        git(workspace, "checkout", "--quiet", "--detach", task.base_commit)
    # Search the object store, including unreachable commits copied from local caches.
    rows = git(
        workspace, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"
    )
    commits = {
        row.split()[0].decode("ascii") for row in rows.splitlines() if row.endswith(b" commit")
    }
    head = git(workspace, "rev-parse", "HEAD").decode("ascii").strip()
    if commits != {task.base_commit} or head != task.base_commit:
        raise ValueError("Task checkout must contain only the pinned base commit")


def visible_runner(
    recipe: Recipe, workspace: Path, assets: Path, *, pipeline_metrics: bool = False
) -> CommandRunner:
    fixed = ["pytest", *recipe.pytest_args]
    mounts = ["--mount", f"type=bind,src={workspace},dst=/work,readonly"]
    if recipe.dependencies:
        dependency = (assets / recipe.dependencies).resolve(strict=True)
        if dependency == assets or not dependency.is_relative_to(assets) or not dependency.is_dir():
            raise ValueError("Dependency directory must be inside the dedicated assets directory")
        mounts += ["--mount", f"type=bind,src={dependency},dst=/deps,readonly"]

    async def run(
        argv: list[str],
        cwd: Path,
        *,
        config: ExecutionConfig | None = None,
        artifacts_dir: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> JSONDict:
        if argv != fixed or cwd != workspace:
            raise ToolError("COMMAND_NOT_CONFIGURED", "Use the configured visible-test tool.")
        name = f"stellarweaver-swetest-{uuid4().hex}"
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=1g",
            "--cpus=2",
            *mounts,
            "--workdir=/tmp",
            "--env=PATH=/opt/miniconda3/envs/testbed/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--env=PYTHONNOUSERSITE=1",
            "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
            "--entrypoint=python",
            recipe.image,
            "-c",
            VISIBLE_PYTEST,
            "-p",
            "no:cacheprovider",
            *recipe.pytest_args,
        ]
        started = perf_counter()
        try:
            result = await execute_command(
                command,
                cwd,
                config=config,
                artifacts_dir=artifacts_dir,
                timeout_seconds=min(timeout_seconds or 120, 120),
            )
        finally:
            executed_at = perf_counter()
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "rm", "--force", name],
                env=controlled_environment(),
                capture_output=True,
                timeout=15,
            )
        result["data"].update(argv=argv, cwd=".", backend="linux_docker")
        if pipeline_metrics:
            result["data"]["sandbox_timing_ms"] = {
                "execute": (executed_at - started) * 1000,
                "cleanup": (perf_counter() - executed_at) * 1000,
            }
        return result

    return run


async def solve_task(
    case: Path,
    attempt: Path,
    args: argparse.Namespace,
    run: RunConfiguration,
    config_sha256: str,
) -> None:
    task = Task.model_validate_json((case / "task.json").read_bytes())
    recipe = Recipe.model_validate_json((case / "recipe.json").read_bytes())
    await asyncio.to_thread(local_image, recipe.image)
    workspace = attempt / "checkout"
    await asyncio.to_thread(git_base, task, args.assets, workspace)
    argv = ["pytest", *recipe.pytest_args]
    config = run.app
    harness = config.harness
    sandbox = run.sandbox_commands
    development = harness.development_tools
    runner: CommandRunner
    if sandbox:
        dependency = None
        if recipe.dependencies:
            dependency = (args.assets / recipe.dependencies).resolve(strict=True)
            if (
                dependency == args.assets
                or not dependency.is_relative_to(args.assets)
                or not dependency.is_dir()
                or any(char in str(dependency) for char in (",", '"'))
            ):
                raise ValueError(
                    "Dependency directory must be inside the dedicated assets directory"
                )
        argv = [
            "python",
            "-I",
            "-B",
            "-c",
            SANDBOX_PYTEST,
            "-p",
            "no:cacheprovider",
            *recipe.pytest_args,
        ]
        runner = DockerRunner(
            workspace,
            Isolation(
                recipe.image, (tuple(argv),), "stellarweaver-swe-fixture", development=development
            ),
            dependency,
            pipeline_metrics=harness.pipeline_metrics,
            incremental_snapshots=harness.incremental_sandbox_snapshots,
        )
    else:
        runner = visible_runner(
            recipe, workspace, args.assets, pipeline_metrics=harness.pipeline_metrics
        )
    policy = attempt / "policy.json"
    grants = [{"tool": "run_command", "argv": argv, "cwd": "."}]
    save(policy, {"workspace": str(workspace), "grants": grants})
    provider = config.model.provider
    model = (
        build_provider(config, workspace, "deepseek", None, args.env_file, stream=True)
        if provider == "deepseek"
        else FakeModel.from_script(
            args.fake_scripts / f"{task.instance_id}.json",
            context_window_tokens=config.model.context_window_tokens,
        )
    )
    try:
        with SessionStore(workspace, data_dir=attempt / "state") as store:
            store.set_config(config.model_dump())
            agent = build_agent(
                config,
                model,
                store,
                policy_file=policy,
                command_runner=runner if sandbox else None,
                command_boundary=digest(canonical_json(recipe.model_dump())) if sandbox else None,
                allow_sandbox_development=development,
                sandbox_commands=(tuple(argv),) if sandbox else None,
                adaptive_round_retention=run.adaptive_round_retention,
                allow_benchmark_staging=harness.patch_staging,
                enforce_exploration_caps=True,
            )
            register_fixed_tools(
                agent.registry,
                runner,
                {
                    "run_visible_tests": (
                        "Run configured tests in the isolated container.",
                        tuple(argv),
                    )
                },
            )
            schemas = agent.registry.schemas()
            allowed = {schema["function"]["name"] for schema in schemas}
            optional = allowed & (
                DEVELOPMENT_TOOLS | {"delegate_exploration", "stage_patch", "apply_patch"}
            )
            prompt = (
                f"Fix {task.instance_id} in {task.repo} at base commit {task.base_commit}. "
                "Inspect the repository, make the smallest correct change, and run run_visible_tests "
                "with {} when useful. Report the change and the actual validation evidence."
            )
            if optional:
                prompt += "\nAvailable additional tools: " + ", ".join(sorted(optional)) + "."
            prompt += "\n\n" + task.problem_statement
            tools_sha256 = digest(canonical_json(schemas))
            outcome = await agent.run(prompt)
            events = store.load_events()
            completed_requests = [event for event in events if event.type == "model.completed"]
            failed_requests = [event for event in events if event.type == "model.failed"]
            all_requests = [
                event for event in events if event.type in {"model.completed", "model.failed"}
            ]
            cached = [
                count
                for event in completed_requests
                if type(count := event.payload.get("prompt_cache_hit_tokens")) is int
            ]
            failure_category = None
            if outcome.run_status in {"blocked", "failed"} and any(
                tool.status == "denied"
                or tool.data.get("code") in HARD_PATH_ERRORS
                or tool.data.get("code")
                in {
                    "PERMISSION_DENIED",
                    "AUTHORIZATION_CHANGED",
                    "HOOK_DENIED",
                    "HOOK_FAILED_CLOSED",
                }
                for tool in agent.results
            ):
                failure_category = "policy_blocked"
            elif (
                outcome.run_status == "failed"
                and all_requests
                and all_requests[-1].type == "model.failed"
            ):
                failure_category = "provider_error"
            evidence = {
                "session_id": store.session_id,
                "outcome": asdict(outcome),
                "tools": [asdict(tool) for tool in agent.results],
                "cached_input_tokens": (
                    sum(cached)
                    if completed_requests and len(cached) == len(completed_requests)
                    else None
                ),
                "failed_model_calls": len(failed_requests),
                "failure_category": failure_category,
            }
    finally:
        if isinstance(model, OpenAICompatibleChatProvider):
            await model.aclose()
    # Intent-to-add exposes new files to the diff without staging their contents.
    git(workspace, "add", "-N", "--", ".")
    patch = git(workspace, "diff", "--no-ext-diff", "--binary", task.base_commit, "--")
    candidate = "empty_patch"
    if patch:
        (attempt / "candidate.patch").write_bytes(patch)
        with tempfile.TemporaryDirectory(prefix="sw-verify-", ignore_cleanup_errors=True) as stage:
            clean = Path(stage) / "checkout"
            await asyncio.to_thread(checkout, workspace, clean, task.base_commit)
            try:
                git(clean, "apply", "--check", str(attempt / "candidate.patch"))
                prediction = Prediction(
                    instance_id=task.instance_id,
                    model_name_or_path=f"stellarweaver-{provider}",
                    model_patch=patch.decode("utf-8"),
                )
            except (subprocess.CalledProcessError, ValueError):
                candidate = "invalid_patch"
            else:
                save(attempt / "prediction.json", prediction.model_dump())
                candidate = "valid_patch"
    save(
        attempt / "agent-report.json",
        {
            **evidence,
            "provider": provider,
            "model": config.model.model,
            "thinking": config.model.thinking,
            "reasoning_effort": config.model.reasoning_effort,
            "prompt_sha256": digest(prompt.encode()),
            "tools_sha256": tools_sha256,
            "config_sha256": config_sha256,
            "policy_sha256": digest(
                canonical_json(
                    {"version": POLICY_VERSION, "preset": config.policy.preset, "grants": grants}
                )
            ),
            "sandbox_profile": "sandbox-workspace" if sandbox else "swebench-visible-pytest-v1",
            "agent_image": recipe.image,
            "patch_sha256": digest(patch) if patch else None,
            "candidate_status": candidate,
        },
    )


def state(case: Path) -> JSONDict:
    pointer = case / "solve.json"
    if not pointer.is_file():
        return {"status": "not_run"}
    value: JSONDict = read_json(pointer)
    if value["status"] == "running":
        attempt = case / value["attempt"]
        if (attempt / "attempt.json").is_file():
            receipt: JSONDict = read_json(attempt / "attempt.json")
            return receipt
        if (attempt / "agent-report.json").is_file():
            report = read_json(attempt / "agent-report.json")
            return {
                **value,
                "status": report["outcome"]["run_status"],
                **{key: report[key] for key in ("candidate_status", "patch_sha256", "provider")},
            }
        return {**value, "status": "interrupted"}
    return value


def finish_attempt(case: Path, result: JSONDict) -> None:
    """Commit the attempt's own receipt before advancing the case pointer."""
    receipt = case / result["attempt"] / "attempt.json"
    if receipt.exists():
        if read_json(receipt) != result:
            raise ValueError(f"Attempt receipt changed for {case.name}")
    else:
        # The batch entry point holds its experiment lock across this transition.
        save(receipt, result)
    save(case / "solve.json", result)


def saved_prediction(case: Path) -> Prediction | None:
    solved = state(case)
    if solved.get("candidate_status") not in {"valid_patch", "provided_patch"}:
        return None
    prediction = Prediction.model_validate_json(
        (case / solved["attempt"] / "prediction.json").read_bytes()
    )
    if (
        prediction.instance_id != case.name
        or digest(prediction.model_patch.encode()) != solved["patch_sha256"]
    ):
        raise ValueError(f"Prediction provenance mismatch for {case.name}")
    return prediction


async def solve(root: Path, args: argparse.Namespace) -> None:
    keys = prepared_tasks(root)
    run = RunConfiguration.from_manifest(args, read_json(root / "manifest.json"))
    settings_path = root / "run-settings.json"
    settings = {
        **run.settings(),
        "assets": str(args.assets),
        "fixtures": {
            key: digest((args.fake_scripts / f"{key}.json").read_bytes())
            for key in keys
            if args.fake_scripts and (args.fake_scripts / f"{key}.json").is_file()
        },
        **execution_fingerprint(),
    }
    settings["config_sha256"] = run_config_sha256(settings)
    if settings_path.exists() and read_json(settings_path) != settings:
        raise ValueError(
            "Run settings changed. Use a new experiment for a different configuration."
        )
    if not settings_path.exists():
        save(settings_path, settings)
    for key in keys:
        case = root / "cases" / key
        previous = state(case)
        prior = previous["status"]
        if prior != "not_run" and not (
            args.retry_interrupted and prior in {"interrupted", "environment_error"}
        ):
            continue
        if prior != "not_run":
            finish_attempt(case, previous)
        attempt = case / f"agent-{uuid4().hex}"
        attempt.mkdir()
        save(case / "solve.json", {"status": "running", "attempt": attempt.name})
        try:
            verify_inputs(root, case, "task.json", "recipe.json")
            await solve_task(case, attempt, args, run, settings["config_sha256"])
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            finish_attempt(
                case,
                {
                    "status": "environment_error",
                    "attempt": attempt.name,
                    "reason": type(exc).__name__,
                },
            )
            print(key, "environment_error", type(exc).__name__, flush=True)
        else:
            finish_attempt(case, state(case))
            print(key, state(case)["status"], flush=True)


def import_predictions(root: Path, source: Path) -> None:
    rows = [
        Prediction.model_validate_json(line)
        for line in source.read_bytes().splitlines()
        if line.strip()
    ]
    keys = {row.instance_id for row in rows}
    allowed = set(prepared_tasks(root))
    if not rows or len(keys) != len(rows) or not keys <= allowed:
        raise ValueError("Imports must contain distinct instance IDs in the prepared selection")
    if any(state(root / "cases" / key)["status"] != "not_run" for key in keys):
        raise ValueError("Import would replace an existing attempt")
    for row in rows:
        case = root / "cases" / row.instance_id
        attempt = case / f"import-{uuid4().hex}"
        attempt.mkdir()
        save(attempt / "prediction.json", row.model_dump())
        finish_attempt(
            case,
            {
                "status": "imported",
                "attempt": attempt.name,
                "candidate_status": "provided_patch",
                "patch_sha256": digest(row.model_patch.encode()),
            },
        )


def evaluate(root: Path, args: argparse.Namespace) -> None:
    selected = set(getattr(args, "instance_id", None) or ())
    if selected - set(prepared_tasks(root)):
        raise ValueError("Unknown requested instance IDs")
    for key in prepared_tasks(root):
        if selected and key not in selected:
            continue
        case = root / "cases" / key
        prediction = saved_prediction(case)
        if prediction is None:
            continue
        path = case / "evaluation.json"
        previous = read_json(path) if path.is_file() else {}
        patch_sha = digest(prediction.model_patch.encode())
        if previous and previous["patch_sha256"] != patch_sha:
            raise ValueError(f"Previously graded patch changed for {key}")
        inputs = (
            ("scorer.json", "recipe.json") if (case / "recipe.json").is_file() else ("scorer.json",)
        )
        verify_inputs(root, case, *inputs)
        public_recipe = read_json(case / "recipe.json") if (case / "recipe.json").is_file() else {}
        oracle = case / "scorer.json"
        scorer = read_json(oracle)
        image = public_recipe.get("grader_image") or scorer.get("image")
        attempt_id = state(case)["attempt"]
        binding = {
            "patch_sha256": patch_sha,
            "image": image,
            "scorer_sha256": digest(oracle.read_bytes()),
            "grader_version": GRADER_VERSION,
        }
        if previous.get("status") in {"resolved", "unresolved"} and not args.rescore:
            if any(previous.get(name) != expected for name, expected in binding.items()) or (
                previous.get("attempt_id", attempt_id) != attempt_id
                or previous.get("instance_id", key) != key
            ):
                raise ValueError(f"Cached score identity mismatch for {key}")
            score_id = previous.get("run_id")
            if not isinstance(score_id, str) or not re.fullmatch(r"grade-[a-f0-9]{32}", score_id):
                raise ValueError(f"Cached score ID is invalid for {key}")
            cached_dir = case / score_id
            if (
                read_json(cached_dir / "evaluation.json") != previous
                or previous.get("raw_report") != f"{score_id}/report.json"
            ):
                raise ValueError(f"Cached score does not match its evidence for {key}")
            saved_bytes = (cached_dir / "report.json").read_bytes()
            saved_sha = digest(saved_bytes)
            saved_report = json.loads(saved_bytes).get(key)
            if (
                previous.get("raw_report_sha256", saved_sha) != saved_sha
                or not isinstance(saved_report, dict)
                or saved_report.get("infra_failure")
                or saved_report.get("resolved") is not (previous["status"] == "resolved")
                or previous.get("resolved") is not saved_report.get("resolved")
            ):
                raise ValueError(f"Cached verdict does not match its report for {key}")
            continue
        result_dir = case / f"grade-{uuid4().hex}"
        result_dir.mkdir()
        result: JSONDict = {
            **binding,
            "status": "evaluation_error",
            "resolved": None,
            "run_id": result_dir.name,
            "attempt_id": attempt_id,
            "instance_id": key,
        }
        save(path, {**result, "status": "evaluating"})
        if not OFFICIAL_REQUIRED <= scorer.keys():
            result["reason"] = "scorer_metadata_missing"
        elif not image or not re.fullmatch(IMAGE_PATTERN, image):
            result["reason"] = "unpinned_grader_image"
        else:
            try:
                local_image(image)
            except (OSError, subprocess.SubprocessError):
                result["reason"] = "grader_image_unavailable"
            else:
                model_patch = case / state(case)["attempt"] / "prediction.json"
                with (result_dir / "grader.log").open("wb") as log:
                    try:
                        subprocess.run(
                            [
                                str(args.grader_python),
                                "-I",
                                # Official harness writes eval.sh/patch.diff via write_text()
                                # without encoding; on non-UTF-8 Windows locales (e.g. GBK)
                                # that corrupts non-ASCII test code. -I ignores PYTHONUTF8,
                                # so force UTF-8 mode explicitly.
                                "-X",
                                "utf8",
                                str(OFFICIAL),
                                "evaluate",
                                str(oracle),
                                str(model_patch),
                                str(result_dir),
                                image,
                                str(args.timeout),
                            ],
                            env=controlled_environment(),
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            timeout=args.timeout + 150,
                            check=True,
                        )
                        raw_report = result_dir / "report.json"
                        raw_bytes = raw_report.read_bytes()
                        official = json.loads(raw_bytes)[key]
                        result["raw_report"] = f"{result_dir.name}/report.json"
                        result["raw_report_sha256"] = digest(raw_bytes)
                        if (
                            official.get("infra_failure")
                            or type(official.get("resolved")) is not bool
                        ):
                            result["reason"] = "grader_infrastructure_failure"
                        else:
                            result.update(
                                status="resolved" if official["resolved"] else "unresolved",
                                resolved=official["resolved"],
                                patch_applied=official.get("patch_successfully_applied"),
                            )
                    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
                        result["reason"] = type(exc).__name__
                    finally:
                        subprocess.run(
                            [
                                "docker",
                                "rm",
                                "--force",
                                f"stellarweaver-swegrade-{result_dir.name}",
                            ],
                            env=controlled_environment(),
                            capture_output=True,
                            timeout=15,
                            check=False,
                        )
        save(result_dir / "evaluation.json", result)
        save(path, result)
        print(key, result["status"], result.get("reason", ""), flush=True)


def report(root: Path) -> JSONDict:
    rows: list[JSONDict] = []
    for key in prepared_tasks(root):
        case = root / "cases" / key
        evaluation = case / "evaluation.json"
        result = read_json(evaluation) if evaluation.is_file() else {"status": "not_evaluated"}
        if result["status"] == "evaluating":
            result = {**result, "status": "evaluation_error", "reason": "evaluation_interrupted"}
        rows.append({"instance_id": key, "agent": state(case), "evaluation": result})
    by_status = {
        status: sum(row["evaluation"]["status"] == status for row in rows)
        for status in ("resolved", "unresolved", "not_evaluated", "evaluation_error")
    }
    summary = {
        "planned": len(rows),
        "by_status": by_status,
        "resolved_rate": by_status["resolved"] / len(rows),
    }
    save(root / "task_results.jsonl", rows, jsonl=True)
    save(root / "aggregate.json", summary)
    return summary


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    subs = cli.add_subparsers(dest="action", required=True)
    prepare_cmd = subs.add_parser(
        "prepare", help="Freeze the selection and separate public and scorer inputs"
    )
    prepare_cmd.add_argument("--dataset", type=Path, required=True)
    prepare_cmd.add_argument("--dataset-sha256", required=True)
    prepare_cmd.add_argument("--split", choices=tuple(SPLIT_COUNT), default="dev")
    prepare_cmd.add_argument("--legacy-dev", action="store_true")
    prepare_cmd.add_argument(
        "--official-cases", type=Path, help="Verified scorer records for a legacy dev replay"
    )
    prepare_cmd.add_argument("--instance-id", action="append")
    prepare_cmd.add_argument("--limit", type=int)
    prepare_cmd.add_argument(
        "--max-model-calls",
        type=int,
        help="Optional positive model-request limit per task (default: no request-count limit)",
    )
    prepare_cmd.add_argument("--recipes", type=Path)
    prepare_cmd.add_argument(
        "--context-window-tokens", type=int, default=DEFAULT_CONTEXT_WINDOW_TOKENS
    )
    prepare_cmd.add_argument(
        "--reasoning-effort",
        choices=("low", "high", "max"),
        default="high",
        help="DeepSeek thinking reasoning_effort frozen in manifest (default: high)",
    )
    for name, default in LIMITS.items():
        prepare_cmd.add_argument(
            "--" + name.replace("_", "-"),
            type=int,
            default=default,
            help=f"Freeze the per-task budget (default: {default})",
        )
    solve_cmd = subs.add_parser("run", help="Run pending tasks with an explicit provider")
    solve_cmd.add_argument(
        "--harness-profile",
        choices=("baseline", "full"),
        default="baseline",
        help="Start with all enhancements disabled (baseline) or enabled (full)",
    )
    for name in (
        "sandbox_commands",
        "adaptive_round_retention",
        "include_linter",
        *_HARNESS_SWITCHES,
    ):
        solve_cmd.add_argument("--" + name.replace("_", "-"), action=argparse.BooleanOptionalAction)
    for name in _EXPLORATION_OPTIONS:
        solve_cmd.add_argument(
            "--" + name.replace("_", "-"),
            type=int,
            help=f"Exploration safety limit (default: {getattr(HarnessConfig(), name)})",
        )
    solve_cmd.add_argument("--fresh-tool-rounds", type=int, default=2)
    solve_cmd.add_argument("--repair-controller", choices=("off", "feedback", "enforce"))
    provider = solve_cmd.add_mutually_exclusive_group(required=True)
    provider.add_argument("--live", action="store_true")
    provider.add_argument("--fake-scripts", type=Path)
    solve_cmd.add_argument("--assets", type=Path, required=True)
    solve_cmd.add_argument("--env-file", type=Path, default=Path(".env"))
    solve_cmd.add_argument("--retry-interrupted", action="store_true")
    import_cmd = subs.add_parser(
        "import", help="Use previously saved predictions without calling the model"
    )
    import_cmd.add_argument("--predictions", type=Path, required=True)
    evaluate_cmd = subs.add_parser("evaluate", help="Score saved predictions in the official image")
    evaluate_cmd.add_argument("--timeout", type=int, default=240)
    evaluate_cmd.add_argument("--rescore", action="store_true")
    evaluate_cmd.add_argument(
        "--instance-id", action="append", help="Only evaluate these prepared tasks"
    )
    export_cmd = subs.add_parser("export", help="Collect candidates in official JSONL order")
    report_cmd = subs.add_parser(
        "report", help="Aggregate from saved runs without calling the model"
    )
    for cmd in (prepare_cmd, solve_cmd, import_cmd, evaluate_cmd, export_cmd, report_cmd):
        cmd.add_argument("--output", type=Path, required=True)
    for cmd in (prepare_cmd, evaluate_cmd):
        cmd.add_argument("--grader-python", type=Path, required=True)
    return cli


def main() -> int:
    cli = parser()
    args = cli.parse_args()
    for name, value in vars(args).items():
        if isinstance(value, Path):
            setattr(args, name, value.resolve())
    root = args.output
    if getattr(args, "limit", None) is not None and args.limit < 1:
        cli.error("--limit must be positive")
    if getattr(args, "timeout", 1) < 1:
        cli.error("--timeout must be positive")
    if args.action != "prepare" and not (root / "manifest.json").is_file():
        cli.error("Prepare the experiment first")
    try:
        with locked(root):
            if args.action == "prepare":
                prepare(args)
            elif args.action == "run":
                asyncio.run(solve(root, args))
            elif args.action == "import":
                import_predictions(root, args.predictions)
            elif args.action == "evaluate":
                evaluate(root, args)
            elif args.action == "export":
                items = [saved_prediction(root / "cases" / key) for key in prepared_tasks(root)]
                save(
                    root / "predictions.jsonl",
                    [item.model_dump() for item in items if item],
                    jsonl=True,
                )
            else:
                print(json.dumps(report(root), indent=2))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        cli.exit(2, f"{exc}\n")
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
