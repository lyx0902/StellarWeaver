import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from stellarweaver.evals import swebench as bench
from stellarweaver.evals import swebench_contract as contract
from stellarweaver.evals.provenance import canonical_json
from stellarweaver.evals.swebench_pilot import git

IMAGE = "sha256:" + "a" * 64
GRADER = "swebench/sweb.eval.x86_64.fixture@" + IMAGE
FIRST = "fixture__alpha-1"
SECOND = "fixture__beta-2"


def source_repo(path: Path) -> str:
    path.mkdir(parents=True)
    (path / "module.py").write_bytes(b"value = 0\n")
    git(path, "init", "-q")
    git(path, "add", ".")
    git(
        path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return git(path, "rev-parse", "HEAD").decode().strip()


def prepare(
    tmp_path: Path,
    base: str,
    max_model_calls: int | None = None,
    *,
    instance_ids: tuple[str, ...] = (FIRST, SECOND),
    image: str = IMAGE,
) -> Path:
    tmp_path.mkdir(exist_ok=True)
    rows = [
        {
            "instance_id": key,
            "repo": "fixture/" + key.split("__")[1].split("-")[0],
            "base_commit": base,
            "problem_statement": "Fix the fixture value.",
            "patch": "ORACLE-ONLY",
            "test_patch": "HIDDEN-TEST",
            "hints_text": "HINT-ONLY",
            "FAIL_TO_PASS": '["test_example"]',
            "PASS_TO_PASS": "[]",
            "version": "test",
            "eval_script": "#!/bin/bash\n",
            "eval_type": "pass_and_fail",
            "log_parser": "parse_log_pytest",
        }
        for key in (*instance_ids, *(f"fixture__unused-{i}" for i in range(23 - len(instance_ids))))
    ]
    dataset = tmp_path / "source.json"
    recipes = tmp_path / "recipes.json"
    bench.save(dataset, rows)
    bench.save(
        recipes,
        {
            key: {"image": image, "pytest_args": ["test_module.py"], "grader_image": GRADER}
            for key in instance_ids
        },
    )
    root = tmp_path / "run"
    root.mkdir()
    bench.prepare(
        argparse.Namespace(
            output=root,
            dataset=dataset,
            dataset_sha256=bench.digest(dataset.read_bytes()),
            split="dev",
            legacy_dev=True,
            grader_python=Path(sys.executable),
            recipes=recipes,
            instance_id=list(instance_ids),
            limit=None,
            max_model_calls=max_model_calls,
        )
    )
    return root


async def test_batch_synthetic_case_uses_shared_development_sandbox(tmp_path: Path) -> None:
    image = os.getenv("STELLARWEAVER_M5_TEST_IMAGE")
    if not image:
        pytest.skip("Requires a cached Linux Python image pinned by ID")
    assets = tmp_path / "assets"
    root = prepare(tmp_path, source_repo(assets / FIRST), instance_ids=(FIRST,), image=image)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    bench.save(
        fixtures / f"{FIRST}.json",
        [
            {
                "stop_reason": "tool_calls",
                "tool_calls": [
                    {
                        "call_id": "syntax",
                        "name": "check_syntax",
                        "arguments_json": '{"target":"module.py"}',
                    }
                ],
            },
            {"text": "The source compiled in the fixture."},
        ],
    )
    await bench.solve(
        root,
        argparse.Namespace(
            live=False,
            fake_scripts=fixtures,
            assets=assets,
            retry_interrupted=False,
            sandbox_commands=True,
            development_tools=True,
            issue_navigation=True,
            repair_controller="feedback",
        ),
    )
    case = root / "cases" / FIRST
    state = bench.state(case)
    assert state["status"] == "completed" and state["candidate_status"] == "empty_patch"
    evidence = bench.read_json(case / state["attempt"] / "agent-report.json")
    assert evidence["sandbox_profile"] == "sandbox-workspace"
    (check,) = evidence["tools"]
    assert check["status"] == "ok" and check["data"]["process_management"]["cleanup_confirmed"]
    assert check["data"]["image_id"] == image and "Syntax check passed" in check["data"]["stdout"]
    assert bench.read_json(root / "run-settings.json")["development_tools"] is True


@pytest.mark.asyncio
async def test_batch_isolated_attempts_resume_and_failed_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assets = tmp_path / "assets"
    base = source_repo(assets / FIRST)
    second = assets / SECOND
    git(assets, "clone", "--no-hardlinks", str(assets / FIRST), str(second))
    root = prepare(tmp_path, base)
    left, right = (root / "cases" / key for key in (FIRST, SECOND))
    assert bench.prepared_tasks(root) == [FIRST, SECOND]
    assert bench.read_json(root / "manifest.json")["dataset_revision_verified"] is False
    public = (left / "task.json").read_text(encoding="utf-8")
    assert "ORACLE-ONLY" not in public and "HIDDEN-TEST" not in public and "HINT-ONLY" not in public
    assert bench.read_json(left / "scorer.json")["patch"] == "ORACLE-ONLY"

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    bench.save(
        fixtures / f"{FIRST}.json",
        [
            {
                "tool_calls": [
                    {
                        "call_id": "read",
                        "name": "read_file",
                        "arguments_json": '{"path":"module.py"}',
                    },
                    {
                        "call_id": "outside",
                        "name": "read_file",
                        "arguments_json": '{"path":"../../scorer.json"}',
                    },
                ],
                "stop_reason": "tool_calls",
            },
            {
                "tool_calls": [
                    {
                        "call_id": "edit",
                        "name": "edit_file",
                        "arguments_json": json.dumps(
                            {
                                "path": "module.py",
                                "old_text": "value = 0",
                                "new_text": "value = 1",
                                "expected_sha256": hashlib.sha256(b"value = 0\n").hexdigest(),
                            }
                        ),
                    }
                ],
                "stop_reason": "tool_calls",
            },
            {"text": "Done."},
        ],
    )
    bench.save(fixtures / f"{SECOND}.json", [{"text": "No changes."}])
    args = argparse.Namespace(
        live=False, fake_scripts=fixtures, assets=assets, retry_interrupted=False
    )
    monkeypatch.setattr(bench, "local_image", lambda image: None)
    abandoned = right / "agent-abandoned"
    abandoned.mkdir()
    bench.save(right / "solve.json", {"status": "running", "attempt": abandoned.name})

    await bench.solve(root, args)
    first = bench.state(left)
    assert first["status"] == "completed"
    assert bench.state(right)["status"] == "interrupted"
    assert bench.saved_prediction(left).model_patch.endswith("+value = 1\n")
    assert not (second / "module.py").read_text(encoding="utf-8").startswith("value = 1")
    assert not (left / first["attempt"] / "checkout" / "scorer.json").exists()
    run = bench.read_json(left / first["attempt"] / "agent-report.json")
    assert next(tool for tool in run["tools"] if tool["call_id"] == "outside")["status"] != "ok"
    with sqlite3.connect(run["outcome"]["state_path"]) as saved:
        messages = " ".join(row[0] for row in saved.execute("SELECT body_json FROM messages"))
    assert "Fix the fixture value." in messages and "ORACLE-ONLY" not in messages
    assert "HINT-ONLY" not in messages and "HIDDEN-TEST" not in messages

    args.retry_interrupted = True
    await bench.solve(root, args)
    assert bench.state(left) == first
    assert bench.state(right)["candidate_status"] == "empty_patch"
    assert bench.saved_prediction(right) is None
    assert abandoned.is_dir()
    assert bench.read_json(abandoned / "attempt.json")["status"] == "interrupted"
    assert bench.read_json(left / first["attempt"] / "attempt.json")["status"] == "completed"

    def missing(_image: str) -> None:
        raise subprocess.CalledProcessError(1, "docker")

    monkeypatch.setattr(bench, "local_image", missing)
    bench.evaluate(root, argparse.Namespace(grader_python=sys.executable, timeout=5, rescore=False))
    assert bench.read_json(left / "evaluation.json")["reason"] == "grader_image_unavailable"
    assert bench.report(root)["by_status"] == {
        "resolved": 0,
        "unresolved": 0,
        "not_evaluated": 1,
        "evaluation_error": 1,
    }
    normalized = contract.read_report(root)
    assert normalized.aggregate["by_candidate_status"] == {"valid_patch": 1, "empty_patch": 1}
    assert normalized.aggregate["by_evaluation_status"] == {
        "evaluation_error": 1,
        "not_evaluated": 1,
    }
    assert {attempt.run_status for attempt in normalized.tasks[1].attempts} == {
        "completed",
        "interrupted",
    }


@pytest.mark.parametrize("model_limit,expected_calls", [(None, 32), (16, 16)])
async def test_batch_uses_frozen_model_call_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_limit: int | None, expected_calls: int
) -> None:
    assets = tmp_path / "assets"
    root = prepare(tmp_path, source_repo(assets / FIRST), model_limit)
    case = root / "cases" / FIRST
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    bench.save(
        fixtures / f"{FIRST}.json",
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"read-{index}",
                        "name": "read_file",
                        "arguments_json": '{"path":"module.py"}',
                    }
                ],
                "stop_reason": "tool_calls",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            }
            for index in range(31)
        ]
        + [{"text": "Done."}],
    )
    monkeypatch.setattr(bench, "local_image", lambda image: None)
    args = argparse.Namespace(
        live=False, fake_scripts=fixtures, assets=assets, retry_interrupted=False
    )
    # Keep the second task pending without provisioning another checkout.
    bench.save(
        root / "cases" / SECOND / "solve.json", {"status": "running", "attempt": "agent-abandoned"}
    )
    await bench.solve(root, args)
    solved = bench.state(case)
    outcome = bench.read_json(case / solved["attempt"] / "agent-report.json")["outcome"]
    assert outcome["model_calls"] == expected_calls
    if model_limit is None:
        assert outcome["run_status"] == "completed"
    else:
        assert (outcome["run_status"], outcome["reason"]) == ("budget_exceeded", "max_model_calls")
    limits = bench.read_json(root / "manifest.json")["limits"]
    assert (
        limits
        == bench.read_json(root / "run-settings.json")["limits"]
        == {**bench.LIMITS, "max_model_calls": model_limit}
    )


def test_checkout_refuses_future_commits(tmp_path: Path) -> None:
    source = tmp_path / "assets" / FIRST
    base = source_repo(source)
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "later",
    )
    task = bench.Task(
        instance_id=FIRST, repo="fixture/alpha", base_commit=base, problem_statement="Fix."
    )
    with pytest.raises(ValueError, match="only the pinned base commit"):
        bench.git_base(task, source.parent, tmp_path / "checkout")


def write_candidate(root: Path, key: str, *, suffix: str = "") -> None:
    path = root.parent / f"{root.parent.name}-{key}.jsonl"
    bench.save(
        path,
        [{"instance_id": key, "model_name_or_path": "fixture", "model_patch": "fixture" + suffix}],
        jsonl=True,
    )
    bench.import_predictions(root, path)


def fixture_score(monkeypatch: pytest.MonkeyPatch, *, resolved: bool = True) -> list[str]:
    calls: list[str] = []

    def grade(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[0] != "docker":
            directory = Path(argv[6])
            row = bench.read_json(Path(argv[5]))
            calls.append(directory.name)
            bench.save(directory / "report.json", {row["instance_id"]: {"resolved": resolved}})
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(bench, "local_image", lambda *_: None)
    monkeypatch.setattr(bench.subprocess, "run", grade)
    return calls


def test_contract_binding_reuses_cache_and_keeps_independent_rescores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = prepare(tmp_path / "original", "b" * 40)
    changed = prepare(tmp_path / "changed", "b" * 40)
    write_candidate(original, FIRST)
    write_candidate(changed, FIRST, suffix="-changed")
    calls = fixture_score(monkeypatch)
    opts = argparse.Namespace(grader_python=sys.executable, timeout=5, rescore=False)
    case = original / "cases" / FIRST
    bench.evaluate(original, opts)
    first_score = bench.read_json(case / "evaluation.json")
    first_receipt = case / first_score["run_id"] / "evaluation.json"
    original_bytes = first_receipt.read_bytes()
    bench.evaluate(original, opts)
    assert calls == [first_score["run_id"]]
    opts.rescore = True
    bench.evaluate(original, opts)
    opts.rescore = False
    bench.evaluate(changed, opts)
    assert len(set(calls)) == 3 and first_receipt.read_bytes() == original_bytes

    report = contract.read_report(original)
    assert report.aggregate["planned"] == 2
    assert report.aggregate["graded"] == 1
    assert report.aggregate["resolved_rate"] == 0.5
    assert len(report.tasks[0].scores) == 2
    assert all(
        score.attempt_id == report.tasks[0].latest_attempt_id for score in report.tasks[0].scores
    )
    assert (
        first_score["patch_sha256"] != contract.read_report(changed).tasks[0].scores[0].patch_sha256
    )

    pointer = case / "evaluation.json"
    corrupted = bench.read_json(pointer)
    corrupt_score_id = corrupted["run_id"]
    corrupted["image"] = "another@" + IMAGE
    bench.save(pointer, corrupted)
    with pytest.raises(ValueError, match="Cached score identity mismatch"):
        bench.evaluate(original, opts)
    with pytest.raises(ValueError, match="disagrees with its receipt"):
        contract.read_report(original)
    # The raw result is bound in the immutable receipt, even if its verdict stayed the same.
    bench.save(pointer, bench.read_json(case / corrupt_score_id / "evaluation.json"))
    bench.save(case / corrupt_score_id / "report.json", {FIRST: {"resolved": True, "extra": 1}})
    with pytest.raises(ValueError, match="Cached verdict does not match its report"):
        bench.evaluate(original, opts)
    with pytest.raises(ValueError, match="report hash mismatch"):
        contract.read_report(original)


def test_contract_full_denominator_deterministic_output_and_pair_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = (FIRST, SECOND, "fixture__budget-3", "fixture__api-4", "fixture__missing-5")
    root = prepare(tmp_path, "b" * 40, instance_ids=keys)
    write_candidate(root, FIRST)
    calls = fixture_score(monkeypatch)
    bench.evaluate(root, argparse.Namespace(grader_python=sys.executable, timeout=5, rescore=False))
    for key, run_status, candidate, cause in (
        (SECOND, "completed", "empty_patch", None),
        (keys[2], "budget_exceeded", "empty_patch", None),
        (keys[3], "failed", "empty_patch", "provider_error"),
    ):
        case = root / "cases" / key
        attempt = case / "agent-saved"
        attempt.mkdir()
        bench.save(
            attempt / "agent-report.json",
            {
                "outcome": {"run_status": run_status},
                "candidate_status": candidate,
                "failure_category": cause,
            },
        )
        bench.finish_attempt(case, {"status": run_status, "attempt": attempt.name})

    report = contract.read_report(root)
    assert report.aggregate["planned"] == 5
    assert report.aggregate["by_failure_category"] == {"provider_error": 1}
    assert report.aggregate["by_run_status"] == {
        "imported": 1,
        "completed": 1,
        "budget_exceeded": 1,
        "failed": 1,
        "not_started": 1,
    }
    assert report.aggregate["by_candidate_status"]["empty_patch"] == 3
    assert report.aggregate["no_patch"] == 3
    assert report.aggregate["not_started"] == 1
    assert report.aggregate["by_evaluation_status"] == {"resolved": 1, "not_evaluated": 4}
    assert report.aggregate["resolved_rate"] == 0.2

    files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    outputs = [tmp_path / "result1.json", tmp_path / "result2.json"]
    for target in outputs:
        monkeypatch.setattr(sys, "argv", ["contract", "report", str(root), "--output", str(target)])
        assert contract.main() == 0
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    assert (
        b"HIDDEN-TEST" not in outputs[0].read_bytes()
        and b"ORACLE-ONLY" not in outputs[0].read_bytes()
    )
    with pytest.raises(SystemExit, match="2"):
        contract.main()
    assert all(path.read_bytes() == initial for path, initial in files.items())
    assert set(files) == {path for path in root.rglob("*") if path.is_file()}
    assert len(calls) == 1
    assert contract.compare(report, report)["paired_graded"] == 0  # import provenance unknown

    task = report.tasks[0]
    original = task.attempt()
    assert original
    controls = {
        "provider": "fake",
        "model_id": "fixture",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "agent_image": IMAGE,
        "sandbox_profile": "fixture",
        **{
            name: "b" * 64
            for name in (
                "config_sha256",
                "policy_sha256",
                "tools_schema_sha256",
                "prompt_sha256",
                "fixture_sha256",
            )
        },
    }
    left = report.model_copy(
        update={
            "tasks": [
                task.model_copy(update={"attempts": [original.model_copy(update=controls)]}),
                *report.tasks[1:],
            ]
        }
    )
    right = report.model_copy(
        update={
            "tasks": [
                task.model_copy(
                    update={
                        "attempts": [
                            original.model_copy(update={**controls, "prompt_sha256": "c" * 64})
                        ],
                    }
                ),
                *report.tasks[1:],
            ]
        }
    )
    assert contract.compare(left, right)["paired_graded"] == 0
    paired = contract.compare(left, right, varied=frozenset({"prompt_sha256"}))
    assert paired["pairs"]["both_resolved"] == paired["paired_graded"] == 1
    assert paired["common_planned"] == 5
    with pytest.raises(ValueError, match="Unsupported experiment variation"):
        contract.compare(left, right, varied=frozenset({"policy_sha256"}))
    assert (
        canonical_json(contract.read_report(root).model_dump(mode="json")) + b"\n"
        == outputs[0].read_bytes()
    )
