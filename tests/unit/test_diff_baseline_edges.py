"""Baseline-only paths remain visible after Git stops reporting them."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from stellarweaver.changes import ChangesManager
from stellarweaver.policy import PolicyEngine
from stellarweaver.session.store import SessionStore


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        [
            "git",
            "--no-pager",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Offline Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            *arguments,
        ],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "中文 baseline repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "tracked.txt").write_bytes(b"committed baseline\n")
    (root / "rename.txt").write_bytes(b"rename fixture content\n")
    git(root, "add", "--", "tracked.txt", "rename.txt")
    # Only this disposable fixture gets a HEAD, so a real clean Git status exists.
    git(root, "commit", "-qm", "temporary test fixture")
    return root


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def other_section(diff: str) -> str:
    return diff.split("Other current changes", 1)[1]


@pytest.mark.parametrize("name", ["user-untracked.txt", "用户 with spaces.txt"])
async def test_removed_untracked_baseline_path_is_observed_without_touching_workspace(
    repository: Path, tmp_path: Path, name: str
) -> None:
    path = repository / name
    path.write_bytes(b"initial user bytes\r\n")
    with SessionStore(repository, tmp_path / "state") as store:
        manager = ChangesManager(repository, store, PolicyEngine(workspace=repository))
        await manager.capture_baseline()
        path.unlink()
        before = snapshot(repository)
        diff = await manager.diff()
        assert name in other_section(diff)
        assert '"state": "missing"' in other_section(diff)
        assert "No additional changes observed" not in other_section(diff)
        assert snapshot(repository) == before


@pytest.mark.parametrize("baseline_kind", ["dirty", "deleted"])
async def test_tracked_path_restored_to_clean_is_compared_to_session_baseline(
    repository: Path, tmp_path: Path, baseline_kind: str
) -> None:
    path = repository / "tracked.txt"
    original = path.read_bytes()
    if baseline_kind == "dirty":
        path.write_bytes(b"user dirty contents\n")
    else:
        path.unlink()
    with SessionStore(repository, tmp_path / "state") as store:
        manager = ChangesManager(repository, store, PolicyEngine(workspace=repository))
        await manager.capture_baseline()
        path.write_bytes(original)
        assert git(repository, "status", "--porcelain=v1") == b""
        before = snapshot(repository)
        tail = other_section(await manager.diff())
        assert "tracked.txt" in tail
        assert "No additional changes observed" not in tail
        assert snapshot(repository) == before


async def test_exact_return_to_original_user_baseline_is_not_a_new_change(
    repository: Path, tmp_path: Path
) -> None:
    path = repository / "tracked.txt"
    baseline = b"user dirty at startup\r\n"
    path.write_bytes(baseline)
    (repository / "user.txt").write_bytes(b"user untracked\n")
    with SessionStore(repository, tmp_path / "state") as store:
        manager = ChangesManager(repository, store, PolicyEngine(workspace=repository))
        await manager.capture_baseline()
        path.write_bytes(b"intermediate external edit\n")
        path.write_bytes(baseline)
        before = snapshot(repository)
        tail = other_section(await manager.diff())
        assert "No additional changes observed" in tail
        assert "\ntracked.txt\n" not in tail
        assert snapshot(repository) == before


async def test_untracked_rename_reports_old_missing_and_new_path(
    repository: Path, tmp_path: Path
) -> None:
    old = repository / "old user.txt"
    old.write_bytes(b"same untracked contents\n")
    with SessionStore(repository, tmp_path / "state") as store:
        manager = ChangesManager(repository, store, PolicyEngine(workspace=repository))
        await manager.capture_baseline()
        old.rename(repository / "new user.txt")
        before = snapshot(repository)
        tail = other_section(await manager.diff())
        assert "old user.txt" in tail and "new user.txt" in tail
        assert '"state": "missing"' in tail
        assert snapshot(repository) == before


async def test_git_rename_preserves_original_path_and_index_bytes(
    repository: Path, tmp_path: Path
) -> None:
    with SessionStore(repository, tmp_path / "state") as store:
        manager = ChangesManager(repository, store, PolicyEngine(workspace=repository))
        await manager.capture_baseline()
        git(repository, "mv", "--", "rename.txt", "renamed.txt")
        before = snapshot(repository)
        tail = other_section(await manager.diff())
        assert "rename.txt" in tail and "renamed.txt" in tail
        assert "Current Git rename: rename.txt -> renamed.txt" in tail
        assert snapshot(repository) == before


async def test_baseline_file_changed_to_empty_directory_is_visible(
    repository: Path, tmp_path: Path
) -> None:
    path = repository / "user-file"
    path.write_bytes(b"user file\n")
    with SessionStore(repository, tmp_path / "state") as store:
        manager = ChangesManager(repository, store, PolicyEngine(workspace=repository))
        await manager.capture_baseline()
        path.unlink()
        path.mkdir()
        before = snapshot(repository)
        tail = other_section(await manager.diff())
        assert "user-file" in tail
        assert '"state": "not_file"' in tail
        assert snapshot(repository) == before


@pytest.mark.parametrize("restore_clean", [False, True])
async def test_existing_baseline_rename_is_not_reported_as_new(
    repository: Path, tmp_path: Path, restore_clean: bool
) -> None:
    git(repository, "mv", "--", "rename.txt", "renamed.txt")
    with SessionStore(repository, tmp_path / "state") as store:
        manager = ChangesManager(repository, store, PolicyEngine(workspace=repository))
        await manager.capture_baseline()
        if restore_clean:
            git(repository, "mv", "--", "renamed.txt", "rename.txt")
            assert git(repository, "status", "--porcelain=v1") == b""
        before = snapshot(repository)
        tail = other_section(await manager.diff())
        if restore_clean:
            assert "rename.txt" in tail and "renamed.txt" in tail
        else:
            assert "No additional changes observed" in tail
            assert "Current Git rename:" not in tail
        assert snapshot(repository) == before
