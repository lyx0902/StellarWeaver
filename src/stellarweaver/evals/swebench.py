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
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from stellarweaver.cli import build_provider, effective_config
from stellarweaver.config import AppConfig, ExecutionConfig, LimitsConfig
from stellarweaver.domain import JSONDict
from stellarweaver.evals.swebench_official import GRADER_VERSION
from stellarweaver.evals.swebench_pilot import checkout, digest, git, register_fixed_tools
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.locking import WriterLock
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import CommandRunner, controlled_environment, execute_command
from stellarweaver.tools.workspace import ToolError

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
LIMITS = {
    "max_tool_calls": 50,
    "max_wall_seconds": 360,
    "max_input_tokens_total": 320_000,
    "max_output_tokens_total": 16_000,
}
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


def execution_fingerprint() -> JSONDict:
    package = Path(__file__).resolve().parents[1]
    components = (
        "evals/swebench.py",
        "evals/swebench_pilot.py",
        "evals/swebench_official.py",
        "runtime/loop.py",
        "runtime/service.py",
        "config.py",
        "tools/registry.py",
        "providers/openai_compatible.py",
    )
    root = package.parents[1]
    revision = git(root, "rev-parse", "HEAD").decode().strip() if (root / ".git").exists() else None
    return {
        "code_sha": revision,
        "source_sha256": {path: digest((package / path).read_bytes()) for path in components},
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
    limits = LimitsConfig.model_validate({**LIMITS, "max_model_calls": args.max_model_calls})
    manifest: JSONDict = {
        "schema_version": 1,
        "dataset": "SWE-bench/SWE-bench_Lite",
        "dataset_revision": None if args.legacy_dev else DATASET_REVISION,
        "dataset_revision_verified": not args.legacy_dev,
        "dataset_sha256": args.dataset_sha256,
        "split": args.split,
        "limits": limits.model_dump(),
        "model": "deepseek-flash",
        "thinking": "enabled",
        "reasoning_effort": "high",
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


def visible_runner(recipe: Recipe, workspace: Path, assets: Path) -> CommandRunner:
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
        try:
            result = await execute_command(
                command,
                cwd,
                config=config,
                artifacts_dir=artifacts_dir,
                timeout_seconds=min(timeout_seconds or 120, 120),
            )
        finally:
            await asyncio.to_thread(
                subprocess.run,
                ["docker", "rm", "--force", name],
                env=controlled_environment(),
                capture_output=True,
                timeout=15,
            )
        result["data"].update(argv=argv, cwd=".", backend="linux_docker")
        return result

    return run


async def solve_task(
    case: Path, attempt: Path, args: argparse.Namespace, limits: LimitsConfig
) -> None:
    task = Task.model_validate_json((case / "task.json").read_bytes())
    recipe = Recipe.model_validate_json((case / "recipe.json").read_bytes())
    await asyncio.to_thread(local_image, recipe.image)
    workspace = attempt / "checkout"
    await asyncio.to_thread(git_base, task, args.assets, workspace)
    runner = visible_runner(recipe, workspace, args.assets)
    argv = ["pytest", *recipe.pytest_args]
    policy = attempt / "policy.json"
    save(
        policy,
        {
            "workspace": str(workspace),
            "grants": [{"tool": "run_command", "argv": argv, "cwd": "."}],
        },
    )
    provider: Literal["deepseek", "fake"] = "deepseek" if args.live else "fake"
    config = effective_config(AppConfig(), provider, "workspace-edit", True)
    config = config.model_copy(update={"limits": limits})
    model = (
        build_provider(config, workspace, "deepseek", None, args.env_file, stream=True)
        if args.live
        else FakeModel.from_script(args.fake_scripts / f"{task.instance_id}.json")
    )
    prompt = (
        f"Fix {task.instance_id} in {task.repo} at base commit {task.base_commit}. "
        "Inspect the repository, make the smallest correct change, and run run_visible_tests "
        "with {} when useful. Report the change and the actual validation evidence.\n\n"
        + task.problem_statement
    )
    try:
        with SessionStore(workspace, data_dir=attempt / "state") as store:
            store.set_config(config.model_dump())
            agent = build_agent(config, model, store, policy_file=policy)
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
            tools_sha256 = digest(json.dumps(agent.registry.schemas(), sort_keys=True).encode())
            outcome = await agent.run(prompt)
            evidence = {
                "session_id": store.session_id,
                "outcome": asdict(outcome),
                "tools": [asdict(tool) for tool in agent.results],
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
        with tempfile.TemporaryDirectory(prefix="verify-", dir=attempt) as stage:
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
        if (attempt / "agent-report.json").is_file():
            report = read_json(attempt / "agent-report.json")
            return {
                **value,
                "status": report["outcome"]["run_status"],
                **{key: report[key] for key in ("candidate_status", "patch_sha256", "provider")},
            }
        return {**value, "status": "interrupted"}
    return value


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
    limits = LimitsConfig.model_validate(read_json(root / "manifest.json")["limits"])
    settings_path = root / "run-settings.json"
    settings = {
        "provider": "deepseek" if args.live else "fake",
        "limits": limits.model_dump(),
        "assets": str(args.assets),
        "fixtures": {
            key: digest((args.fake_scripts / f"{key}.json").read_bytes())
            for key in keys
            if args.fake_scripts and (args.fake_scripts / f"{key}.json").is_file()
        },
        **execution_fingerprint(),
    }
    if settings_path.exists() and read_json(settings_path) != settings:
        raise ValueError(
            "Run settings changed. Use a new experiment for a different configuration."
        )
    if not settings_path.exists():
        save(settings_path, settings)
    for key in keys:
        case = root / "cases" / key
        prior = state(case)["status"]
        if prior != "not_run" and not (
            args.retry_interrupted and prior in {"interrupted", "environment_error"}
        ):
            continue
        attempt = case / f"agent-{uuid4().hex}"
        attempt.mkdir()
        save(case / "solve.json", {"status": "running", "attempt": attempt.name})
        try:
            verify_inputs(root, case, "task.json", "recipe.json")
            await solve_task(case, attempt, args, limits)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            save(
                case / "solve.json",
                {
                    "status": "environment_error",
                    "attempt": attempt.name,
                    "reason": type(exc).__name__,
                },
            )
            print(key, "environment_error", type(exc).__name__, flush=True)
        else:
            save(case / "solve.json", state(case))
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
        save(
            case / "solve.json",
            {
                "status": "imported",
                "attempt": attempt.name,
                "candidate_status": "provided_patch",
                "patch_sha256": digest(row.model_patch.encode()),
            },
        )


def evaluate(root: Path, args: argparse.Namespace) -> None:
    for key in prepared_tasks(root):
        case = root / "cases" / key
        prediction = saved_prediction(case)
        if prediction is None:
            continue
        path = case / "evaluation.json"
        previous = read_json(path) if path.is_file() else {}
        patch_sha = digest(prediction.model_patch.encode())
        if previous and previous["patch_sha256"] != patch_sha:
            raise ValueError(f"Previously graded patch changed for {key}")
        if previous.get("status") in {"resolved", "unresolved"} and not args.rescore:
            continue
        inputs = (
            ("scorer.json", "recipe.json") if (case / "recipe.json").is_file() else ("scorer.json",)
        )
        verify_inputs(root, case, *inputs)
        result_dir = case / f"grade-{uuid4().hex}"
        result_dir.mkdir()
        public_recipe = read_json(case / "recipe.json") if (case / "recipe.json").is_file() else {}
        oracle = case / "scorer.json"
        scorer = read_json(oracle)
        image = public_recipe.get("grader_image") or scorer.get("image")
        result: JSONDict = {
            "status": "evaluation_error",
            "resolved": None,
            "run_id": result_dir.name,
            "patch_sha256": patch_sha,
            "image": image,
            "scorer_sha256": digest(oracle.read_bytes()),
            "grader_version": GRADER_VERSION,
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
                        official = read_json(result_dir / "report.json")[key]
                        result["raw_report"] = f"{result_dir.name}/report.json"
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
    solve_cmd = subs.add_parser("run", help="Run pending tasks with an explicit provider")
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
