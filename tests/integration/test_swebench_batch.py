import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from stellarweaver.evals import swebench as bench
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


def prepare(tmp_path: Path, base: str) -> Path:
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
        for key in (FIRST, SECOND, *(f"fixture__unused-{i}" for i in range(21)))
    ]
    dataset = tmp_path / "source.json"
    recipes = tmp_path / "recipes.json"
    bench.save(dataset, rows)
    bench.save(
        recipes,
        {
            key: {"image": IMAGE, "pytest_args": ["test_module.py"], "grader_image": GRADER}
            for key in (FIRST, SECOND)
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
            instance_id=[FIRST, SECOND],
            limit=None,
        )
    )
    return root


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
                    }
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
    run = bench.read_json(left / first["attempt"] / "agent-report.json")
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
