"""Scoring receipts identify and preserve exact rule and implementation bytes."""

import json
from pathlib import Path

import pytest

from stellarweaver.evals import catalog, score_report, sha
from stellarweaver.evals.provenance import archive, identity, verify, write_receipt


def saved_answer(tmp_path: Path) -> Path:
    task = next(task for task in catalog() if task["task_id"] == "Q01")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    for name, body in task["files"].items():
        (workspace / name).write_text(body, encoding="utf-8", newline="\n")
    report = tmp_path / "report.json"
    body = {
        "catalog_sha256": "historical-before-rule-change",
        "cases": [
            {
                "task_id": "Q01",
                "workspace": str(workspace),
                "outcome": {"final_text": "config.py resolve CLI environment file default"},
                "tools": [
                    {
                        "tool_name": "read_file",
                        "status": "ok",
                        "data": {
                            "path": "config.py",
                            "sha256": sha(task["files"]["config.py"].encode()),
                        },
                    }
                ],
            }
        ],
    }
    report.write_text(json.dumps(body), encoding="utf-8")
    return report


def test_receipt_links_report_current_catalog_and_archived_sources(tmp_path: Path) -> None:
    report = saved_answer(tmp_path)
    result = score_report(report)
    assert result["passed"] == result["attempted"] == 1 and result["model_calls"] == 0
    assert result["source_report_sha256"] == sha(report.read_bytes())
    assert result["source_catalog_sha256"] != result["scoring_catalog_sha256"]
    assert result["scoring_catalog_sha256"] == identity()["catalog_sha256"]
    receipt = tmp_path / "receipt.json"
    write_receipt(result, receipt)
    public = json.loads(receipt.read_text(encoding="utf-8"))
    bundle = tmp_path / public["provenance_bundle"]
    assert verify(bundle, public["provenance"]) == result["provenance"]
    original = receipt.read_bytes()
    with pytest.raises(FileExistsError):
        write_receipt(result, receipt)
    assert receipt.read_bytes() == original
    code = next(bundle.glob("*-__init__.py"))
    code.write_bytes(code.read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify(bundle, public["provenance"])


def test_explicit_historical_catalog_reproduces_rule_difference(tmp_path: Path) -> None:
    report = saved_answer(tmp_path)
    historical = catalog()
    for task in historical:
        task["scoring_rules"].pop("required_sources", None)
    path = tmp_path / "historical.json"
    path.write_text(json.dumps(historical), encoding="utf-8")
    previous = score_report(report, catalog_path=path)
    current = score_report(report)
    assert previous["passed"] == 0 and current["passed"] == 1
    assert previous["model_calls"] == current["model_calls"] == 0
    assert previous["scoring_catalog_sha256"] == sha(path.read_bytes())
    receipt = tmp_path / "old-rules-receipt.json"
    write_receipt(previous, receipt, path)
    verify(receipt.with_name(receipt.name + ".provenance"), previous["provenance"])


def test_source_drift_refuses_to_archive_inconsistent_identity(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog()), encoding="utf-8")
    before = identity(path)
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="changed during grading"):
        archive(tmp_path / "bundle", before, path)
    assert not (tmp_path / "bundle").exists()


def test_cli_refuses_existing_receipt_before_running_candidate_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    import stellarweaver.evals.__main__ as cli

    output = tmp_path / "existing.json"
    output.write_bytes(b"keep receipt")
    monkeypatch.setattr(
        sys, "argv", ["evals", "score", "missing-report.json", "--output", str(output)]
    )

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("grading must not run before checking receipt destination")

    monkeypatch.setattr(cli, "score_report", unexpected)
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert output.read_bytes() == b"keep receipt"


async def test_suite_refuses_catalog_changed_between_selection_and_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stellarweaver.evals as evaluation

    # Redirect only the package catalog/source lookups to a private copy; do not
    # mutate the real package or run a model while reproducing the capture race.
    package = tmp_path / "package"
    package.mkdir()
    source = Path(evaluation.__file__)
    for name in ("__init__.py", "__main__.py", "provenance.py", "catalog.json"):
        (package / name).write_bytes(source.with_name(name).read_bytes())
    monkeypatch.setattr(evaluation, "__file__", str(package / "__init__.py"))
    import stellarweaver.evals.provenance as provenance

    monkeypatch.setattr(provenance, "__file__", str(package / "provenance.py"))
    original = evaluation.load_config

    def config_after_catalog_edit(*args: object, **kwargs: object):
        path = package / "catalog.json"
        path.write_bytes(path.read_bytes() + b"\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluation, "load_config", config_after_catalog_edit)
    destination = tmp_path / "run"
    with pytest.raises(ValueError, match="changed during grading"):
        await evaluation.run_suite(output=destination, task_ids=["Q01"])
    assert not (destination / "scoring-provenance").exists()
    assert not (destination / "Q01").exists()
    assert not (destination / "report.json").exists()


def test_score_refuses_catalog_changed_during_identity_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stellarweaver.evals as evaluation

    report = saved_answer(tmp_path)
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(catalog()), encoding="utf-8")
    original = evaluation.scoring_identity

    def changed_identity(catalog_path: Path | None = None):
        path.write_bytes(path.read_bytes() + b"\n")
        return original(catalog_path)

    monkeypatch.setattr(evaluation, "scoring_identity", changed_identity)
    with pytest.raises(ValueError, match="changed while capturing"):
        score_report(report, catalog_path=path)
