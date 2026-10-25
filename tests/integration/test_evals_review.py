"""Independent review regressions for oracle completion and scoring integrity."""

import json
from pathlib import Path

import pytest

from stellarweaver.evals import catalog, grade, score_report, summarize


def candidate(tmp_path: Path) -> tuple[dict, Path]:
    task = next(item for item in catalog() if item["task_id"] == "B01")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    for name, content in task["files"].items():
        (workspace / name).write_text(content, encoding="utf-8", newline="\n")
    return task, workspace


def test_private_oracle_must_reach_its_checks_before_success(tmp_path: Path) -> None:
    task, workspace = candidate(tmp_path)
    (workspace / "target.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    checked = grade(task, workspace)
    assert not checked["oracle_passed"], checked
    assert not checked["passed"]


def test_private_oracle_import_side_effects_cannot_escape_allowed_diff(tmp_path: Path) -> None:
    task, workspace = candidate(tmp_path)
    correct = task["reference"]["target.py"]
    (workspace / "target.py").write_text(
        correct + "\nfrom pathlib import Path\n"
        "Path(__file__).with_name('preserve.txt').write_text('overwritten during oracle')\n",
        encoding="utf-8",
        newline="\n",
    )
    checked = grade(task, workspace)
    assert "preserve.txt" in checked["forbidden_paths"], checked
    assert not checked["passed"]


def test_unknown_rescore_task_is_an_error_instead_of_zero_case_success(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"cases": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="[Uu]nknown|[Nn]ot.*[Ff]ound|[Mm]issing"):
        score_report(report, ["B99-typo"])


def test_denied_tool_attempts_do_not_dilute_actual_execution_error_rate() -> None:
    case = {
        "category": "bug",
        "verification_argv": ["test"],
        "grade": {"passed": False, "valid_diff": False},
        "tools": [
            {"tool_name": "run_command", "status": "error", "data": {"exit_code": 1}},
            {"tool_name": "edit_file", "status": "denied", "data": {"code": "PERMISSION_DENIED"}},
        ],
        "outcome": {
            "run_status": "blocked",
            "model_calls": 1,
            "tool_calls": 2,
            "input_tokens": 0,
            "output_tokens": 0,
            "duration_ms": 0,
        },
    }
    metrics = summarize([case], provider="deepseek")
    assert metrics["permission_denied"] == 1
    assert metrics["tool_error_rate"] == {"numerator": 1, "denominator": 1}
