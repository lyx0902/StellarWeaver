"""Independent scoring, reference scripts and benchmark denominators."""

import json
from collections import Counter
from pathlib import Path

import pytest

from stellarweaver.evals import catalog, grade, run_suite, score_report, sha, summarize


def materialize(task: dict, folder: Path, *, fixed: bool = False) -> Path:
    folder.mkdir(parents=True)
    for name, body in {**task["files"], **(task["reference"] if fixed else {})}.items():
        (folder / name).write_text(body, encoding="utf-8", newline="\n")
    return folder


def test_catalog_has_twenty_fixed_independent_fixtures() -> None:
    tasks = catalog()
    assert len({task["task_id"] for task in tasks}) == 20
    assert Counter(task["category"] for task in tasks) == {
        "bug": 8,
        "feature": 4,
        "cross-file": 4,
        "test": 2,
        "understanding": 2,
    }
    for task in tasks:
        compile(task["oracle"], task["task_id"], "exec")
        assert {
            "fixture_repo",
            "policy_file",
            "model_config",
            "limits",
            "oracle_entrypoint",
            "scoring_rules",
        } <= task.keys()
        for name, body in task["files"].items():
            public = Path(__file__).parents[2] / task["fixture_repo"] / name
            assert public.read_bytes() == body.encode("utf-8")
        assert "oracle" not in task["files"] and "reference" not in task["files"]


async def test_twenty_reference_scripts_real_tools_and_independent_rescore(tmp_path: Path) -> None:
    report_path = await run_suite(output=tmp_path / "reference-suite")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    metrics = report["metrics"]
    assert metrics["measurement_kind"] == "offline_reference_script_infrastructure"
    assert metrics["model_capability_claim"] is False
    assert metrics["attempted"] == metrics["success"] == 20
    assert metrics["failed"] == metrics["blocked"] == metrics["skipped"] == 0
    assert metrics["effective_diff"] == {"numerator": 18, "denominator": 18}
    assert metrics["verification_execution"] == {"numerator": 18, "denominator": 18}
    assert metrics["tool_error_rate"]["numerator"] == 0
    independent = score_report(report_path)
    assert independent["passed"] == independent["attempted"] == 20
    assert independent["model_calls"] == 0
    # A model's earlier success text cannot keep a now-broken candidate green.
    case = next(case for case in report["cases"] if case["task_id"] == "B01")
    (Path(case["workspace"]) / "target.py").write_text(
        "def parse_size(value, default=20): return 999\n"
    )
    again = score_report(report_path, ["B01"])
    assert again["attempted"] == 1 and again["passed"] == 0


@pytest.mark.parametrize("task_id", ["T01", "T02"])
def test_added_tests_must_detect_fixed_mutations(task_id: str, tmp_path: Path) -> None:
    task = next(task for task in catalog() if task["task_id"] == task_id)
    workspace = materialize(task, tmp_path / "repo", fixed=True)
    result = grade(task, workspace)
    assert result["passed"] and result["mutation_detected"]
    for name in task["reference"]:
        (workspace / name).write_text(
            "import unittest\nclass Empty(unittest.TestCase):\n    def test_true(self): self.assertTrue(True)\n"
        )
    assert not grade(task, workspace)["oracle_passed"]


def test_visible_test_changes_do_not_change_private_oracle(tmp_path: Path) -> None:
    task = catalog()[0]
    workspace = materialize(task, tmp_path / "repo")
    (workspace / "test_visible.py").write_text("")
    result = grade(task, workspace)
    assert not result["oracle_passed"] and not result["valid_diff"]
    assert result["forbidden_paths"] == ["test_visible.py"]


def test_repository_answer_requires_observed_hashes_and_ordered_facts(tmp_path: Path) -> None:
    task = next(task for task in catalog() if task["task_id"] == "Q01")
    workspace = materialize(task, tmp_path / "repo")
    text = "config.py resolve: CLI > environment > file > default"
    assert not grade(task, workspace, final_text=text)["passed"]
    observations = [
        {
            "tool_name": "read_file",
            "status": "ok",
            "data": {"path": name, "sha256": sha(body.encode())},
        }
        for name, body in task["files"].items()
        if name.endswith(".py")
    ]
    assert grade(task, workspace, final_text=text, tools=observations)["passed"]
    assert not grade(
        task,
        workspace,
        final_text="config.py resolve file environment CLI default",
        tools=observations,
    )["passed"]


def test_failed_and_blocked_cases_remain_in_denominator() -> None:
    cases = []
    for status in ["completed", "blocked", "failed"]:
        cases.append(
            {
                "category": "bug",
                "grade": {"passed": status == "completed", "valid_diff": status == "completed"},
                "verification_argv": ["test"],
                "tools": [],
                "outcome": {
                    "run_status": status,
                    **dict.fromkeys(
                        [
                            "model_calls",
                            "tool_calls",
                            "input_tokens",
                            "output_tokens",
                            "duration_ms",
                        ],
                        0,
                    ),
                },
            }
        )
    metrics = summarize(cases, provider="deepseek")
    assert metrics["attempted"] == 3 and metrics["success"] == 1 and metrics["failed"] == 2
    assert metrics["blocked"] == 1 and metrics["effective_diff"]["denominator"] == 3


def test_config_explanation_requires_the_resolver_source_not_an_unrelated_import(
    tmp_path: Path,
) -> None:
    task = next(task for task in catalog() if task["task_id"] == "Q01")
    workspace = materialize(task, tmp_path / "repo")
    observed = [
        {
            "tool_name": "read_file",
            "status": "ok",
            "data": {"path": "config.py", "sha256": sha(task["files"]["config.py"].encode())},
        }
    ]
    assert grade(
        task, workspace, final_text="config.py resolve CLI environment file default", tools=observed
    )["passed"]
