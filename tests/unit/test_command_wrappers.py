"""Exit 0/1 must not erase terminal command execution states."""

from pathlib import Path
from typing import Any

import pytest

from stellarweaver.tools.git import GitDiffArgs, GitStatusArgs, prepare_git_diff, prepare_git_status
from stellarweaver.tools.registry import ToolContext
from stellarweaver.tools.search import SearchTextArgs, _rg_search
from stellarweaver.tools.workspace import Workspace


@pytest.mark.parametrize("tool", ["search_text", "git_status", "git_diff"])
@pytest.mark.parametrize(
    ("status", "exit_code", "code"),
    [
        ("unknown", 0, "JOB_CLEANUP_UNCONFIRMED"),
        ("unknown", 1, "JOB_CLEANUP_UNCONFIRMED"),
        ("cancelled", 1, "COMMAND_CANCELLED"),
        ("error", 1, "PROCESS_CONTAINMENT_FAILED"),
        ("error", 1, "COMMAND_TIMEOUT"),
    ],
)
async def test_command_state_survives_wrapper_without_second_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    status: str,
    exit_code: int,
    code: str,
) -> None:
    raw: dict[str, Any] = {
        "status": status,
        "content": "Original execution evidence",
        "artifact_ids": ("original-output",),
        "data": {
            "code": code,
            "exit_code": exit_code,
            "stdout": "",
            "stderr": "",
            "timed_out": code == "COMMAND_TIMEOUT",
            "cancelled": status == "cancelled",
            "output_incomplete": False,
            "stdout_truncated": False,
            "process_management": {"cleanup_confirmed": False},
        },
    }
    calls = 0

    async def execute(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert calls == 1, "An unconfirmed configuration query must stop the next subprocess"
        return raw

    monkeypatch.setattr("stellarweaver.tools.commands.execute_command", execute)
    monkeypatch.setattr("stellarweaver.tools.git.execute_command", execute)
    context = ToolContext(Workspace(tmp_path), tmp_path / "artifacts")
    if tool == "search_text":
        result = await _rg_search("rg", SearchTextArgs(pattern="needle"), context, tmp_path)
    elif tool == "git_status":
        result = await (await prepare_git_status(GitStatusArgs(), context)).execute()
    else:
        result = await (await prepare_git_diff(GitDiffArgs(), context)).execute()
    assert result is raw
    assert calls == 1
