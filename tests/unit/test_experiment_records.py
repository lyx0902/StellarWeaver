import json
from pathlib import Path

from stellarweaver.web.experiment_records import PublishedExperiments


def saved_report(resolved: bool) -> dict:
    sha = "a" * 64
    attempt = {
        "attempt_id": "agent-run",
        "run_status": "completed",
        "candidate_status": "valid_patch",
        "patch_sha256": sha,
        "usage_source": "reported",
        "evidence": [],
        "provider": "fake",
        "model_id": "fixture",
        "thinking": "enabled",
        "reasoning_effort": "high",
        "config_sha256": sha,
        "policy_sha256": sha,
        "tools_schema_sha256": sha,
        "prompt_sha256": sha,
        "sandbox_profile": "sandbox-workspace",
        "agent_image": sha,
        "fixture_sha256": sha,
    }
    key = "fixture__app-1"
    return {
        "manifest": {
            "experiment_id": "fixture",
            "suite": "swebench_lite",
            "suite_revision": "fixture-revision",
            "revision_verified": True,
            "dataset_sha256": sha,
            "split": "dev",
            "instance_ids": [key],
            "harness_commit": sha,
            "source_sha256": {"source": sha},
            "grader_version": "fixture-grader",
            "limits": {},
            "frozen_config_sha256": sha,
            "hints": False,
            "web_search": False,
            "completion_gate": False,
            "evidence": [],
        },
        "tasks": [
            {
                "instance_id": key,
                "git_commit": sha,
                "latest_attempt_id": "agent-run",
                "latest_score_id": "grade-fixture",
                "attempts": [attempt],
                "scores": [
                    {
                        "evaluation_run_id": "grade-fixture",
                        "attempt_id": "agent-run",
                        "evaluation_status": "resolved" if resolved else "unresolved",
                        "resolved": resolved,
                        "patch_sha256": sha,
                        "grader_version": "fixture-grader",
                        "grader_image": sha,
                        "scorer_sha256": sha,
                        "evidence": [],
                    }
                ],
                "evidence": [],
            }
        ],
        "aggregate": {"graded": 999},
    }


def test_published_pairing_keeps_frozen_controls_and_recomputes_totals(tmp_path: Path) -> None:
    folder = tmp_path / "reports"
    folder.mkdir()
    left, right = saved_report(False), saved_report(True)

    def write() -> None:
        for key, report in {"before": left, "after": right}.items():
            (folder / f"{key}.json").write_text(json.dumps(report), encoding="utf-8")

    write()
    library = PublishedExperiments(tmp_path / "report.json")
    assert library.detail("before")["report"]["aggregate"]["graded"] == 1
    comparison = library.comparison("before", "after", frozenset())
    assert comparison["paired_graded"] == comparison["pairs"]["right_only_resolved"] == 1
    right["manifest"]["limits"] = {"max_tool_calls": 1}
    write()
    difference = library.comparison("before", "after", frozenset())
    assert difference["paired_graded"] == 0
    assert difference["control_differences"] == ["limits"]
