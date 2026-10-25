"""Exercise demo prerequisites from a user-like PATH and verify actual tool outcomes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import pytest

from stellarweaver import demo
from stellarweaver.tools import git
from stellarweaver.tools.registry import PreparedTool, ToolContext


def _report(output: str) -> dict[str, object]:
    return json.loads(output[output.index("{") :])


@pytest.mark.parametrize("module", ["stellarweaver.demo", "stellarweaver.extensions.demo"])
def test_missing_git_reports_dependency_before_creating_workspace(
    tmp_path: Path, module: str
) -> None:
    empty_path = tmp_path / "empty-path"
    temporary = tmp_path / "temporary"
    empty_path.mkdir()
    temporary.mkdir()
    environment = {
        **os.environ,
        "PATH": str(empty_path),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "TMPDIR": str(temporary),
        "PYTHONUTF8": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "git" in result.stderr.lower()
    assert "Traceback" not in result.stdout + result.stderr
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(2, "Git executable disappeared after discovery"),
        subprocess.CalledProcessError(
            128, ["git", "init"], stderr=b"Git refused to initialize the test workspace"
        ),
    ],
    ids=["missing-after-discovery", "git-init-failed"],
)
def test_git_initialization_failure_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise failure

    monkeypatch.setattr(demo.subprocess, "run", fail)
    assert demo.initialize_demo_repository(tmp_path, "git") is False
    captured = capsys.readouterr()
    assert "git" in captured.err.lower()
    assert "Traceback" not in captured.out + captured.err
    assert list(tmp_path.iterdir()) == []


async def test_real_demo_observes_failure_and_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(demo.tempfile, "tempdir", str(tmp_path))
    assert await demo.demo() == 0
    report = _report(capsys.readouterr().out)
    assert report["demo_passed"] is True
    assert report["test_exit_codes"] == [1, 0]
    assert report["unexpected_tool_failures"] == []
    workspace = Path(str(report["workspace"]))
    assert workspace.is_relative_to(tmp_path)
    assert "return a + b\n" in (workspace / "calc.py").read_text(encoding="utf-8")
    assert (workspace / "keep.txt").read_bytes() == b"Existing user content\r\n"


async def test_demo_rejects_git_diff_failure_after_tests_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def broken_diff(args: git.GitDiffArgs, context: ToolContext) -> PreparedTool:
        async def execute() -> dict[str, object]:
            return {
                "status": "error",
                "content": "Injected Git diff failure",
                "artifact_ids": (),
                "data": {"code": "INJECTED_GIT_DIFF_FAILURE"},
            }

        return PreparedTool({}, execute)

    monkeypatch.setattr(git, "prepare_git_diff", broken_diff)
    monkeypatch.setattr(demo.tempfile, "tempdir", str(tmp_path))
    assert await demo.demo() == 1
    report = _report(capsys.readouterr().out)
    assert report["demo_passed"] is False
    assert report["test_exit_codes"] == [1, 0]
    assert "git_diff" in json.dumps(report["unexpected_tool_failures"])
