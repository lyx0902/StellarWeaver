from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from stellarweaver.tools.git import GitDiffArgs, GitStatusArgs, prepare_git_diff, prepare_git_status
from stellarweaver.tools.registry import ToolContext
from stellarweaver.tools.workspace import ToolError, Workspace


def git(directory: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args],
        cwd=directory,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return completed.stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Offline Test")
    git(tmp_path, "config", "user.email", "offline@example.invalid")
    (tmp_path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("original\n", encoding="utf-8")
    git(tmp_path, "add", "--", "tracked.txt", "other.txt")
    git(tmp_path, "commit", "-qm", "fixture baseline")
    return tmp_path


def context(directory: Path) -> ToolContext:
    return ToolContext(Workspace(directory), directory / ".stellarweaver" / "artifacts")


async def test_status_distinguishes_staged_unstaged_untracked(repository: Path) -> None:
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    (repository / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (repository / "中文 with spaces.txt").write_text("new\n", encoding="utf-8")
    before = (repository / ".git" / "index").read_bytes()
    prepared = await prepare_git_status(GitStatusArgs(), context(repository))
    result = await prepared.execute()
    assert result["status"] == "ok"
    assert result["data"]["staged"] == ["tracked.txt"]
    assert result["data"]["unstaged"] == ["tracked.txt"]
    assert "中文 with spaces.txt" in result["data"]["untracked"]
    assert (repository / ".git" / "index").read_bytes() == before


async def test_diff_separates_staged_and_unstaged(repository: Path) -> None:
    (repository / "tracked.txt").write_text("staged\n", encoding="utf-8")
    git(repository, "add", "tracked.txt")
    (repository / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    prepared = await prepare_git_diff(GitDiffArgs(paths=["tracked.txt"]), context(repository))
    result = await prepared.execute()
    assert "+unstaged" in result["data"]["diff"]
    assert "-staged" in result["data"]["diff"]
    staged = await prepare_git_diff(
        GitDiffArgs(paths=["tracked.txt"], staged=True), context(repository)
    )
    staged_result = await staged.execute()
    assert "+staged" in staged_result["data"]["diff"]
    assert "-baseline" in staged_result["data"]["diff"]


async def test_diff_limits_paths_and_supports_deleted_file(repository: Path) -> None:
    (repository / "tracked.txt").unlink()
    (repository / "other.txt").write_text("changed\n", encoding="utf-8")
    prepared = await prepare_git_diff(GitDiffArgs(paths=["tracked.txt"]), context(repository))
    result = await prepared.execute()
    assert "deleted file" in result["data"]["diff"]
    assert "other.txt" not in result["data"]["diff"]


async def test_git_ignores_external_diff_textconv_fsmonitor_and_environment(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    marker = repository / "external-ran"
    command = f"\"{os.sys.executable}\" -c \"from pathlib import Path; Path('external-ran').write_text('bad')\""
    git(repository, "config", "diff.external", command)
    git(repository, "config", "core.fsmonitor", command)
    git(repository, "config", "diff.bad.textconv", command)
    (repository / ".gitattributes").write_text("*.txt diff=bad\n", encoding="utf-8")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", command)
    monkeypatch.setenv("GIT_PAGER", command)
    status = await prepare_git_status(GitStatusArgs(), context(repository))
    assert (await status.execute())["status"] == "ok"
    diff = await prepare_git_diff(GitDiffArgs(), context(repository))
    result = await diff.execute()
    assert result["status"] == "ok"
    assert "+changed" in result["data"]["diff"]
    assert not marker.exists()


async def test_unfiltered_diff_omits_protected_files(repository: Path) -> None:
    (repository / ".env").write_text("KEY=old\n", encoding="utf-8")
    git(repository, "add", ".env")
    git(repository, "commit", "-qm", "sensitive fixture")
    (repository / ".env").write_text("KEY=secret-must-not-leak\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("allowed change\n", encoding="utf-8")
    prepared = await prepare_git_diff(GitDiffArgs(), context(repository))
    result = await prepared.execute()
    assert "+allowed change" in result["data"]["diff"]
    assert "secret-must-not-leak" not in result["content"]
    assert result["data"]["omitted_protected_paths"] == [".env"]


@pytest.mark.parametrize("setting", ["clean", "process"])
async def test_git_queries_disable_attribute_filter_programs(
    repository: Path, setting: str
) -> None:
    (repository / "tracked.txt").write_text("changed content\n", encoding="utf-8")
    (repository / ".gitattributes").write_text("*.txt filter=dangerous\n", encoding="utf-8")
    command = f"\"{os.sys.executable}\" -c \"from pathlib import Path; Path('filter-ran').write_text('bad')\""
    git(repository, "config", f"filter.dangerous.{setting}", command)
    git(repository, "config", "filter.dangerous.required", "true")
    status = await prepare_git_status(GitStatusArgs(), context(repository))
    assert (await status.execute())["status"] == "ok"
    diff = await prepare_git_diff(GitDiffArgs(), context(repository))
    result = await diff.execute()
    assert result["status"] == "ok"
    assert "+changed content" in result["data"]["diff"]
    assert not (repository / "filter-ran").exists()


async def test_git_pathspec_magic_is_literal(repository: Path) -> None:
    (repository / "tracked.txt").write_text("changed\n", encoding="utf-8")
    prepared = await prepare_git_diff(GitDiffArgs(paths=["*.txt"]), context(repository))
    result = await prepared.execute()
    assert result["data"]["diff"] == ""


async def test_status_rename_has_original_path(repository: Path) -> None:
    git(repository, "mv", "tracked.txt", "renamed.txt")
    prepared = await prepare_git_status(GitStatusArgs(), context(repository))
    result = await prepared.execute()
    assert result["data"]["entries"][0]["original_path"] == "tracked.txt"
    assert result["data"]["staged"] == ["renamed.txt"]


async def test_non_git_workspace_returns_structured_error(tmp_path: Path) -> None:
    prepared = await prepare_git_status(GitStatusArgs(), context(tmp_path))
    result = await prepared.execute()
    assert result["status"] == "error"
    assert result["data"]["code"] == "GIT_ERROR"
    assert result["data"]["exit_code"] != 0


@pytest.mark.parametrize("path", ["../outside", ".git/config", ".env"])
async def test_diff_rejects_unsafe_paths(repository: Path, path: str) -> None:
    with pytest.raises(ToolError):
        await prepare_git_diff(GitDiffArgs(paths=[path]), context(repository))
