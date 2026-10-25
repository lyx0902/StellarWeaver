"""M4 undo operates on journalled snapshots and preserves a dirty Git baseline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from stellarweaver.changes import ChangesManager
from stellarweaver.domain import AssistantReply, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.locking import SessionBusy, workspace_write_lock
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def dirty_repo(tmp_path: Path) -> Path:
    root = tmp_path / "中文 dirty repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "value.py").write_bytes(b"value = 1\n")
    git(root, "add", "value.py")
    (root / "value.py").write_bytes(b"value = 2\n")
    (root / "user-untracked.txt").write_bytes(b"user baseline\r\n")
    (root / ".env").write_bytes(b"PRIVATE_KEY=do-not-read\n")
    return root


def edit(old: int, new: int) -> AssistantReply:
    before = f"value = {old}\n".encode()
    return AssistantReply(
        tool_calls=(
            ToolCall(
                f"edit-{old}",
                "edit_file",
                json.dumps(
                    {
                        "path": "value.py",
                        "expected_sha256": hashlib.sha256(before).hexdigest(),
                        "old_text": str(old),
                        "new_text": str(new),
                    }
                ),
            ),
        ),
        stop_reason="tool_calls",
    )


async def apply_edits(root: Path, store: SessionStore, *replies: AssistantReply) -> None:
    model = FakeModel([*replies, AssistantReply("Changes applied")])
    registry = ToolRegistry(
        root, PolicyEngine("workspace-edit", workspace=root), artifacts_dir=store.artifacts_dir
    )
    outcome = await Agent(model, registry, store=store).run("Change the requested file")
    assert outcome.run_status == "completed"


async def test_two_undos_restore_user_dirty_bytes_without_touching_index(tmp_path: Path) -> None:
    root = dirty_repo(tmp_path)
    staged = git(root, "diff", "--cached", "--no-ext-diff")
    index_bytes = (root / ".git" / "index").read_bytes()
    before = (root / "value.py").read_bytes()
    with SessionStore(root, tmp_path / "state") as store:
        policy = PolicyEngine("workspace-edit", workspace=root)
        events = []
        manager = ChangesManager(root, store, policy, on_event=events.append)
        baseline = await manager.capture_baseline()
        assert baseline["capture_phase"] == "startup"
        assert baseline["status"]["staged"] == ["value.py"]
        assert baseline["status"]["unstaged"] == ["value.py"]
        assert baseline["status"]["untracked"] == ["user-untracked.txt"]
        assert "PRIVATE_KEY" not in json.dumps(baseline)
        await apply_edits(root, store, edit(2, 3), edit(3, 4))
        assert (root / "value.py").read_bytes() == b"value = 4\n"
        first = await manager.undo()
        assert first["status"] == "ok"
        assert (root / "value.py").read_bytes() == b"value = 3\n"
        second = await manager.undo()
        assert (
            second["status"] == "ok"
            and second["target_operation_id"] != first["target_operation_id"]
        )
        assert (root / "value.py").read_bytes() == before
        assert (await manager.undo())["code"] == "UNDO_EMPTY"
        assert events[0].type == "changes.baseline"
        assert [event.type for event in events].count("changes.undo.started") == 2
        assert [event.type for event in events].count("changes.undo.completed") == 2
    assert (root / ".git" / "index").read_bytes() == index_bytes
    assert git(root, "diff", "--cached", "--no-ext-diff") == staged
    assert (root / "user-untracked.txt").read_bytes() == b"user baseline\r\n"


async def test_undo_created_file_only_deletes_matching_agent_file(tmp_path: Path) -> None:
    root = dirty_repo(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        manager = ChangesManager(root, store, PolicyEngine("workspace-edit", workspace=root))
        await manager.capture_baseline()
        create = AssistantReply(
            tool_calls=(
                ToolCall(
                    "create",
                    "create_file",
                    json.dumps({"path": "new.txt", "content": "agent created\n"}),
                ),
            ),
            stop_reason="tool_calls",
        )
        await apply_edits(root, store, create)
        assert (root / "new.txt").exists()
        assert (await manager.undo())["status"] == "ok"
        assert not (root / "new.txt").exists()
        assert (root / "user-untracked.txt").exists()


@pytest.mark.parametrize(
    "change,code",
    [("file", "UNDO_CONFLICT"), ("backup", "UNDO_BACKUP_CHANGED"), ("mode", "UNDO_MODE_CHANGED")],
)
async def test_undo_refuses_user_file_backup_or_mode_changes(
    tmp_path: Path, change: str, code: str
) -> None:
    root = dirty_repo(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        manager = ChangesManager(root, store, PolicyEngine("workspace-edit", workspace=root))
        await manager.capture_baseline()
        await apply_edits(root, store, edit(2, 3))
        if change == "file":
            (root / "value.py").write_bytes(b"user changed after agent\n")
        elif change == "backup":
            backup = Path(store.load_results()[-1].data["before_artifact_id"])
            await asyncio.to_thread(backup.write_bytes, b"corrupt")
        else:
            (root / "value.py").chmod(stat.S_IREAD)
        before = (root / "value.py").read_bytes()
        try:
            result = await manager.undo()
            assert result["status"] == "blocked" and result["code"] == code
            assert (root / "value.py").read_bytes() == before
            assert not any(event.type == "changes.undo.started" for event in store.load_events())
        finally:
            (root / "value.py").chmod(stat.S_IREAD | stat.S_IWRITE)


async def test_undo_rechecks_current_policy_without_tty(tmp_path: Path) -> None:
    root = dirty_repo(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        manager = ChangesManager(root, store, PolicyEngine(workspace=root))
        await manager.capture_baseline()
        await apply_edits(root, store, edit(2, 3))
        result = await manager.undo()
        assert result["status"] == "blocked" and result["code"] == "PERMISSION_DENIED"
        assert (root / "value.py").read_bytes() == b"value = 3\n"


async def test_diff_distinguishes_baseline_recorded_edits_and_external_changes(
    tmp_path: Path,
) -> None:
    root = dirty_repo(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        manager = ChangesManager(root, store, PolicyEngine("workspace-edit", workspace=root))
        await manager.capture_baseline()
        await apply_edits(root, store, edit(2, 3))
        (root / "shell-created.txt").write_bytes(b"unattributed shell output\n")
        result = await manager.diff()
        assert "User baseline at session start" in result
        assert "Agent built-in file changes" in result
        assert "Other current changes" in result
        assert "shell-created.txt" in result
        assert "value = 1" in result and "value = 2" in result and "value = 3" in result
        assert "PRIVATE_KEY" not in result
        assert (root / "value.py").read_bytes() == b"value = 3\n"


def test_real_process_workspace_lock_excludes_other_sessions(tmp_path: Path) -> None:
    root = dirty_repo(tmp_path)
    script = """
import sys
from pathlib import Path
from stellarweaver.session.locking import SessionBusy, workspace_write_lock
try:
    with workspace_write_lock(Path(sys.argv[1])):
        print('acquired')
except SessionBusy:
    print('busy')
"""
    with workspace_write_lock(root):
        with pytest.raises(SessionBusy):
            with workspace_write_lock(root):
                pass
        child = subprocess.run(
            [sys.executable, "-c", script, str(root)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        assert child.stdout.strip() == "busy"
    child = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert child.stdout.strip() == "acquired"


@pytest.mark.parametrize("recovery_route", ["slash", "session"])
async def test_undo_write_crash_is_reconciled_without_applying_it_twice(
    tmp_path: Path, recovery_route: str
) -> None:
    root = dirty_repo(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        manager = ChangesManager(root, store, PolicyEngine("workspace-edit", workspace=root))
        await manager.capture_baseline()
        await apply_edits(root, store, edit(2, 3))
        database = store.path
    script = """
import asyncio, os, sys
from stellarweaver.changes import ChangesManager
from stellarweaver.policy import PolicyEngine
from stellarweaver.session.store import SessionStore
store = SessionStore.open(sys.argv[1])
original = store.append_event
def crash(kind, payload):
    if kind == 'changes.undo.completed':
        os._exit(91)
    return original(kind, payload)
store.append_event = crash
manager = ChangesManager(store.workspace, store, PolicyEngine('workspace-edit',workspace=store.workspace))
asyncio.run(manager.undo())
raise RuntimeError('did not reach fault injection')
"""
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(database)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 91, child.stderr
    before = (root / "value.py").read_bytes(), (root / "value.py").stat().st_mtime_ns
    with SessionStore.open(database) as store:
        manager = ChangesManager(root, store, PolicyEngine("workspace-edit", workspace=root))
        if recovery_route == "slash":
            result = await manager.undo()
            assert result["status"] == "ok" and result["recovered_from_state"] is True
        else:
            model = FakeModel(
                [AssistantReply("Must not request a model during completed recovery")]
            )
            registry = ToolRegistry(root, manager.policy, artifacts_dir=store.artifacts_dir)
            outcome = await Agent(model, registry, store=store).resume()
            assert outcome.run_status == "completed" and not model.requests
            completed = [
                event for event in store.load_events() if event.type == "changes.undo.completed"
            ]
            assert completed[-1].payload["recovered_from_state"] is True
        assert (await manager.undo())["code"] == "UNDO_EMPTY"
    assert ((root / "value.py").read_bytes(), (root / "value.py").stat().st_mtime_ns) == before
    assert before[0] == b"value = 2\n"


async def test_non_git_workspace_reports_limits_and_keeps_undo_available(tmp_path: Path) -> None:
    root = tmp_path / "not-git"
    root.mkdir()
    (root / "value.py").write_bytes(b"value = 2\n")
    with SessionStore(root, tmp_path / "state") as store:
        manager = ChangesManager(root, store, PolicyEngine("workspace-edit", workspace=root))
        baseline = await manager.capture_baseline()
        assert baseline["git_available"] is False
        await apply_edits(root, store, edit(2, 3))
        assert "Git status is unavailable" in await manager.diff()
        assert (await manager.undo())["status"] == "ok"
        assert (root / "value.py").read_bytes() == b"value = 2\n"


async def test_observable_edit_during_undo_authorization_is_preserved(tmp_path: Path) -> None:
    root = dirty_repo(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        await apply_edits(root, store, edit(2, 3))

        async def approval(invocation: dict[str, object]) -> str:
            assert invocation["action"] == "undo"
            (root / "value.py").write_bytes(b"user edit while approval dialog was open\n")
            return "once"

        policy = PolicyEngine(workspace=root, interactive=True, approval=approval)
        manager = ChangesManager(root, store, policy)
        result = await manager.undo()
        assert result["status"] == "blocked" and result["code"] == "UNDO_CONFLICT"
        assert (root / "value.py").read_bytes() == b"user edit while approval dialog was open\n"
        assert not any(event.type == "changes.undo.started" for event in store.load_events())


@pytest.mark.parametrize(
    "kind", ["edit-applied", "edit-unexecuted", "edit-conflict", "edit-mode", "create-applied"]
)
async def test_session_reconcile_coordinates_pending_undo_without_writing_files(
    tmp_path: Path, kind: str
) -> None:
    root = dirty_repo(tmp_path)
    with SessionStore(root, tmp_path / "state") as store:
        if kind.startswith("create"):
            create = AssistantReply(
                tool_calls=(
                    ToolCall(
                        "created",
                        "create_file",
                        json.dumps({"path": "new.txt", "content": "created\n"}),
                    ),
                ),
                stop_reason="tool_calls",
            )
            await apply_edits(root, store, create)
        else:
            await apply_edits(root, store, edit(2, 3))
        original = store.load_results()[-1]
        metadata = original.data
        path = root / metadata["path"]
        intent = {
            "undo_id": "interrupted-undo",
            "target_operation_id": original.operation_id,
            "path": metadata["path"],
            "expected_sha256": metadata["after_sha256"],
            "restore_sha256": metadata.get("before_sha256"),
            "mode": metadata["mode"],
            "action": "delete_created_file"
            if kind.startswith("create")
            else "restore_before_snapshot",
        }
        store.append_event("changes.undo.started", intent)
        if kind in {"edit-applied", "edit-mode"}:
            path.write_bytes(b"value = 2\n")
        elif kind == "edit-conflict":
            path.write_bytes(b"user wrote another value\n")
        elif kind == "create-applied":
            path.unlink()
        if kind == "edit-mode":
            path.chmod(stat.S_IREAD)
        database = store.path
    before = path.read_bytes() if path.exists() else None
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    try:
        with SessionStore.open(database) as store:
            report = store.reconcile()
            undo_reports = [
                operation for operation in report["operations"] if operation["tool_name"] == "undo"
            ]
            assert len(undo_reports) == 1
            succeeded = kind in {"edit-applied", "create-applied"}
            assert report["requires_attention"] is not succeeded
            assert undo_reports[0]["data"]["recovered_from_state"] is succeeded
            assert store.reconcile()["requires_attention"] is not succeeded
            if succeeded:
                manager = ChangesManager(
                    root, store, PolicyEngine("workspace-edit", workspace=root)
                )
                assert (await manager.undo())["code"] == "UNDO_EMPTY"
        assert (path.read_bytes() if path.exists() else None) == before
        assert (stat.S_IMODE(path.stat().st_mode) if path.exists() else None) == mode
    finally:
        if path.exists():
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
