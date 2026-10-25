"""Fixed fixtures and independent scoring; no API calls occur on import."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from stellarweaver.config import ContextConfig, LimitsConfig, get_api_key, load_config
from stellarweaver.domain import AssistantReply, JSONDict, ToolCall
from stellarweaver.evals.provenance import archive as archive_provenance
from stellarweaver.evals.provenance import identity as scoring_identity
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import controlled_environment
from stellarweaver.tools.registry import ToolRegistry


def _parse_catalog(raw: bytes) -> list[JSONDict]:
    data: Any = json.loads(raw)
    if not isinstance(data, list) or len(data) != 20:
        raise ValueError("Expected the versioned 20-task catalog")
    return data


def catalog(path: Path | None = None) -> list[JSONDict]:
    return _parse_catalog((path or Path(__file__).with_name("catalog.json")).read_bytes())


def _scoring_catalog(path: Path | None = None) -> tuple[list[JSONDict], JSONDict]:
    # Parse exactly the bytes whose identity will be recorded. A second read in
    # identity/archive may reject concurrent edits; it must not silently relabel
    # an already parsed task set with a newer catalog hash.
    raw = (path or Path(__file__).with_name("catalog.json")).read_bytes()
    tasks = _parse_catalog(raw)
    provenance = scoring_identity(path)
    if sha(raw) != provenance["catalog_sha256"]:
        raise ValueError("Scoring catalog changed while capturing its identity")
    return tasks, provenance


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in {".git", "__pycache__", ".pytest_cache"} for part in relative.parts):
            continue
        if path.is_symlink():
            result[relative.as_posix()] = "SYMLINK:" + os.readlink(path)
        elif path.is_file():
            result[relative.as_posix()] = sha(path.read_bytes())
    return result


def process(argv: list[str], cwd: Path, *, timeout: float = 20) -> JSONDict:
    environment = controlled_environment()
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timed_out": True, "stdout": "", "stderr": "oracle deadline"}
    return {
        "exit_code": result.returncode,
        "timed_out": False,
        "stdout": result.stdout[-16384:],
        "stderr": result.stderr[-16384:],
    }


def changed_paths(task: JSONDict, workspace: Path) -> tuple[list[str], list[str]]:
    before = {name: sha(body.encode("utf-8")) for name, body in task["files"].items()}
    after = file_hashes(workspace)
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    forbidden = sorted(set(changed) - set(task["allowed_paths"]))
    return changed, forbidden


def grade(
    task: JSONDict, workspace: Path, *, final_text: str = "", tools: list[JSONDict] | None = None
) -> JSONDict:
    """Run private behavior checks outside the exposed workspace, never a reference diff."""
    changed, forbidden = changed_paths(task, workspace)
    valid_diff = not forbidden
    if task["category"] != "understanding":
        valid_diff &= bool(changed)
    if task["category"] == "cross-file":
        valid_diff &= len(changed) >= 2
    result: JSONDict = {
        "task_id": task["task_id"],
        "changed_paths": changed,
        "forbidden_paths": forbidden,
        "valid_diff": valid_diff,
    }
    category = task["category"]
    if category == "understanding":
        observed = {
            tool.get("data", {}).get("path"): tool.get("data", {}).get("sha256")
            for tool in tools or []
            if tool.get("tool_name") == "read_file" and tool.get("status") == "ok"
        }
        required_sources = task["scoring_rules"].get(
            "required_sources", [name for name in task["files"] if name.endswith(".py")]
        )
        citations = all(
            observed.get(name) == sha((workspace / name).read_bytes()) for name in required_sources
        )
        facts = all(fact.casefold() in final_text.casefold() for fact in task["facts"])
        if task["task_id"] == "Q01":
            words = ("cli", "environment", "file", "default")
            positions = [final_text.casefold().find(word) for word in words]
            facts &= positions == sorted(positions) and min(positions) >= 0
        result.update(
            {
                "oracle_passed": bool(citations and facts),
                "facts_matched": facts,
                "sources_verified": citations,
                "manual_review_required": False,
                "rubric": "exact factual anchors and observed source hashes; no LLM grader",
            }
        )
    elif category == "test":
        # Candidate tests must pass the good implementation and reject a fixed mutation.
        with tempfile.TemporaryDirectory(prefix="stellarweaver-oracle-") as directory:
            clone = Path(directory) / "repo"
            shutil.copytree(
                workspace,
                clone,
                ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
            )
            good = process([sys.executable, "-B", "-m", "unittest", "-q"], clone)
            mutation = (
                "def valid_limit(value):\n    return isinstance(value,int) and 0 <= value <= 100\n"
                if task["task_id"] == "T01"
                else "def add_item(value, items=[]):\n    items.append(value)\n    return items\n"
            )
            (clone / "target.py").write_text(mutation, encoding="utf-8", newline="\n")
            bad = process([sys.executable, "-B", "-m", "unittest", "-q"], clone)
        detected = bad["exit_code"] not in {None, 0}
        result.update(
            {
                "oracle_passed": good["exit_code"] == 0 and detected,
                "good_implementation": good,
                "mutated_implementation": bad,
                "mutation_detected": detected,
            }
        )
    else:
        with tempfile.TemporaryDirectory(prefix="stellarweaver-oracle-") as directory:
            oracle = Path(directory) / "oracle.py"
            marker = "STELLARWEAVER_ORACLE_" + uuid4().hex
            script = (
                "import sys\nfrom pathlib import Path\nsys.dont_write_bytecode=True\n"
                "sys.path.insert(0, str(Path(sys.argv[1]).resolve()))\n"
                + task["oracle"]
                + "\nprint("
                + repr(marker)
                + ")\n"
            )
            oracle.write_text(script, encoding="utf-8", newline="\n")
            checked = process([sys.executable, "-I", "-B", str(oracle), str(workspace)], workspace)
        result.update(
            {
                "oracle_passed": checked["exit_code"] == 0
                and marker in checked["stdout"].splitlines(),
                "oracle_check": checked,
            }
        )
    # Candidate imports/tests can themselves write. Re-check the actual final bytes.
    changed, forbidden = changed_paths(task, workspace)
    valid_diff = not forbidden
    if category != "understanding":
        valid_diff &= bool(changed)
    if category == "cross-file":
        valid_diff &= len(changed) >= 2
    result.update(
        {"changed_paths": changed, "forbidden_paths": forbidden, "valid_diff": valid_diff}
    )
    result["passed"] = bool(result["oracle_passed"] and result["valid_diff"])
    return result


def scripted_model(task: JSONDict, argv: list[str]) -> FakeModel:
    """A reference action script exercises infrastructure, never measures LLM capability."""
    replies = []
    calls = [ToolCall("list", "list_files", "{}")]
    calls.extend(
        ToolCall("read-" + str(index), "read_file", json.dumps({"path": name}))
        for index, name in enumerate(task["files"])
        if name.endswith(".py")
    )
    replies.append(AssistantReply(tool_calls=tuple(calls), stop_reason="tool_calls"))
    if task["category"] != "understanding":
        replies.append(
            AssistantReply(
                tool_calls=(ToolCall("before", "run_command", json.dumps({"argv": argv})),),
                stop_reason="tool_calls",
            )
        )
    for index, (name, replacement) in enumerate(task["reference"].items()):
        before = task["files"].get(name)
        arguments = (
            {"path": name, "content": replacement}
            if before is None
            else {
                "path": name,
                "expected_sha256": sha(before.encode("utf-8")),
                "old_text": before,
                "new_text": replacement,
            }
        )
        replies.append(
            AssistantReply(
                tool_calls=(
                    ToolCall(
                        "edit-" + str(index),
                        "create_file" if before is None else "edit_file",
                        json.dumps(arguments),
                    ),
                ),
                stop_reason="tool_calls",
            )
        )
    if task["category"] != "understanding":
        replies.append(
            AssistantReply(
                tool_calls=(ToolCall("after", "run_command", json.dumps({"argv": argv})),),
                stop_reason="tool_calls",
            )
        )
    text = (
        " ".join(task["facts"])
        if task["facts"]
        else "Reference action script finished; independent grading remains separate."
    )
    replies.append(AssistantReply(text=text))
    return FakeModel(replies)


def summarize(cases: list[JSONDict], *, provider: str) -> JSONDict:
    attempted = len(cases)
    coding = [case for case in cases if case["category"] != "understanding"]
    tools = [tool for case in cases for tool in case["tools"]]
    behavioral = sum(bool(case["grade"]["passed"]) for case in cases)
    successful = sum(
        bool(case["grade"]["passed"]) and case["outcome"]["run_status"] == "completed"
        for case in cases
    )
    resources = {}
    for name in ("model_calls", "tool_calls", "input_tokens", "output_tokens", "duration_ms"):
        values = [case["outcome"][name] for case in cases]
        resources[name] = {
            "mean": statistics.mean(values) if values else 0,
            "median": statistics.median(values) if values else 0,
        }
    return {
        "measurement_kind": "offline_reference_script_infrastructure"
        if provider == "fake"
        else "live_model_task_evaluation",
        "model_capability_claim": provider != "fake",
        "attempted": attempted,
        "success": successful,
        "independent_behavior_success": behavioral,
        "success_definition": "independent oracle plus valid diff plus completed agent run",
        "failed": attempted - successful,
        "blocked": sum(case["outcome"]["run_status"] == "blocked" for case in cases),
        "skipped": 0,
        "task_success_rate": successful / attempted if attempted else None,
        "effective_diff": {
            "numerator": sum(bool(case["grade"]["valid_diff"]) for case in coding),
            "denominator": len(coding),
        },
        "verification_execution": {
            "numerator": sum(
                any(
                    tool["tool_name"] == "run_command"
                    and tool["status"] not in {"blocked", "denied", "cancelled"}
                    and "exit_code" in tool["data"]
                    and tool["data"].get("argv") == case["verification_argv"]
                    for tool in case["tools"]
                )
                for case in coding
            ),
            "denominator": len(coding),
        },
        "tool_error_rate": {
            "numerator": sum(tool["status"] == "error" for tool in tools),
            "denominator": sum(
                tool["status"] not in {"denied", "blocked", "cancelled"} for tool in tools
            ),
        },
        "permission_denied": sum(tool["status"] in {"denied", "blocked"} for tool in tools),
        "cancelled": sum(tool["status"] == "cancelled" for tool in tools),
        "resources": resources,
        "recovery_rate": None,
        "recovery_note": "Robustness fault injections are reported separately by pytest, not in the 20-task denominator.",
        "cost": None,
        "cost_note": "No inferred monetary price; see recorded usage and estimated flags.",
    }


async def run_suite(
    *,
    provider: str = "fake",
    task_ids: list[str] | None = None,
    output: Path | None = None,
    env_file: Path | None = None,
) -> Path:
    tasks, provenance = _scoring_catalog()
    selected = [task for task in tasks if not task_ids or task["task_id"] in task_ids]
    if task_ids and set(task_ids) - {task["task_id"] for task in selected}:
        raise ValueError("Unknown task ID")
    if provider not in {"fake", "deepseek"}:
        raise ValueError("Provider must be fake or deepseek")
    settings = load_config(Path.cwd())
    model_config = settings.model.model_copy(
        update={"provider": "deepseek", "supports_streaming": True, "max_output_tokens": 2048}
    )
    key = get_api_key(model_config, Path.cwd(), env_file) if provider == "deepseek" else None
    if provider == "deepseek" and not key:
        raise ValueError(
            f"API key missing; fill {env_file or Path.cwd() / '.env'} with {model_config.api_key_env}"
        )
    destination = (
        output or Path(tempfile.gettempdir()) / ("stellarweaver-evals-" + uuid4().hex[:10])
    ).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    archive_provenance(destination / "scoring-provenance", provenance)
    cases: list[JSONDict] = []
    started = time.monotonic()
    for task in selected:
        case_dir = destination / task["task_id"]
        workspace = case_dir / "repo"
        workspace.mkdir(parents=True)
        for name, body in task["files"].items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8", newline="\n")
        for command in (["git", "init", "-q"], ["git", "add", "--", *task["files"]]):
            initialized = await asyncio.to_thread(process, command, workspace)
            if initialized["exit_code"]:
                raise RuntimeError("Temporary fixture Git initialization failed")
        argv = [sys.executable, "-B", "-m", "unittest", "-q"]
        grants = [
            {"tool": "edit_file" if name in task["files"] else "create_file", "paths": [name]}
            for name in task["allowed_paths"]
        ]
        if task["category"] != "understanding":
            grants.append({"tool": "run_command", "argv": argv, "cwd": "."})
        policy = case_dir / "policy.json"
        policy.write_text(
            json.dumps({"workspace": str(workspace), "grants": grants}), encoding="utf-8"
        )
        baseline = await asyncio.to_thread(grade, task, workspace)
        if baseline["oracle_passed"] and task["category"] != "understanding":
            raise RuntimeError(
                f"Fixture {task['task_id']} no longer reproduces its expected defect"
            )
        model = (
            scripted_model(task, argv)
            if provider == "fake"
            else OpenAICompatibleChatProvider(model_config, str(key))
        )
        try:
            with SessionStore(workspace, data_dir=destination / "state") as store:
                snapshot = settings.model_copy(
                    update={
                        "model": model_config
                        if provider == "deepseek"
                        else settings.model.model_copy(update={"provider": "fake"})
                    }
                )
                store.set_config(snapshot.model_dump())
                registry = ToolRegistry(
                    workspace,
                    PolicyEngine(
                        "read-only" if task["category"] == "understanding" else "ask",
                        workspace=workspace,
                        policy_file=policy,
                    ),
                    artifacts_dir=store.artifacts_dir,
                )
                agent = Agent(
                    model,
                    registry,
                    LimitsConfig(**task["limits"]),
                    store=store,
                    max_output_tokens=2048,
                    stream=True,
                    context_config=ContextConfig(recent_complete_rounds=3),
                )
                prompt = task["task_prompt"]
                if task["category"] != "understanding":
                    prompt += (
                        "\nThe ONLY authorized executable argv is: "
                        + json.dumps(argv)
                        + ". Run it to verify. Do not run extra python -c, git commands or other argv; "
                        "use the built-in git_diff tool for diffs. Independent behavioral checks run outside your workspace afterwards."
                    )
                else:
                    prompt += "\nUse read_file to verify the source. Do not execute commands or modify files."
                outcome = await agent.run(prompt)
                tools = [asdict(tool) for tool in agent.results]
                checked = await asyncio.to_thread(
                    grade, task, workspace, final_text=outcome.final_text, tools=tools
                )
                case = {
                    "task_id": task["task_id"],
                    "category": task["category"],
                    "workspace": str(workspace),
                    "fixture_hashes": {
                        name: sha(body.encode("utf-8")) for name, body in task["files"].items()
                    },
                    "baseline_oracle": baseline,
                    "verification_argv": argv,
                    "outcome": asdict(outcome),
                    "tools": tools,
                    "grade": checked,
                }
                cases.append(case)
        finally:
            if isinstance(model, OpenAICompatibleChatProvider):
                await model.aclose()
        # Incremental reports preserve failures and denominators if a later task is interrupted.
        report = {
            "schema_version": 1,
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "provider": provider,
            "model": model_config.model if provider == "deepseek" else "scripted-reference",
            "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
            "catalog_sha256": provenance["catalog_sha256"],
            "scoring_provenance": provenance,
            "provenance_bundle": "scoring-provenance",
            "requested_task_ids": [item["task_id"] for item in selected],
            "cases": cases,
            "duration_seconds": time.monotonic() - started,
            "metrics": summarize(cases, provider=provider),
        }
        (destination / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return destination / "report.json"


def score_report(
    path: Path, task_ids: list[str] | None = None, *, catalog_path: Path | None = None
) -> JSONDict:
    report_bytes = path.read_bytes()
    report: JSONDict = json.loads(report_bytes)
    selected_catalog, provenance = _scoring_catalog(catalog_path)
    tasks = {task["task_id"]: task for task in selected_catalog}
    available = {case["task_id"] for case in report["cases"]}
    if task_ids and set(task_ids) - available:
        raise ValueError("Unknown or missing requested task in the report")
    if not available:
        raise ValueError("Cannot score an empty report")
    selected = set(task_ids or available)
    if selected - tasks.keys():
        raise ValueError("Unknown or missing requested task in the scoring catalog")
    scores = [
        grade(
            tasks[case["task_id"]],
            Path(case["workspace"]),
            final_text=case["outcome"]["final_text"],
            tools=case["tools"],
        )
        for case in report["cases"]
        if not task_ids or case["task_id"] in task_ids
    ]
    return {
        "source_report": str(path.resolve()),
        "source_report_sha256": sha(report_bytes),
        "source_catalog_sha256": report.get("catalog_sha256"),
        "scoring_catalog_sha256": provenance["catalog_sha256"],
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "provenance": provenance,
        "model_calls": 0,
        "scores": scores,
        "passed": sum(bool(score["passed"]) for score in scores),
        "attempted": len(scores),
    }
