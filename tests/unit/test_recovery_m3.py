"""Failure injection: reconciliation inspects bytes and never repeats effects."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from stellarweaver.domain import Message, ToolCall, ToolResult
from stellarweaver.session.store import SessionStore


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def planned(store: SessionStore, name: str, identifier: str = "call") -> tuple[str, str]:
    assistant = store.record_assistant(
        Message("assistant", tool_calls=(ToolCall(identifier, name, "{}"),))
    )
    return assistant, store.operation_id_for(assistant, identifier)


def started_file(store: SessionStore, name: str = "edit_file", **extra: object) -> tuple[str, str]:
    assistant, operation = planned(store, name)
    store.record_approved(
        assistant, {"operation_id": operation, "call_id": "call", "decision": "allow"}
    )
    store.record_intent(
        assistant,
        {
            "operation_id": operation,
            "call_id": "call",
            "tool": name,
            "path": "value.txt",
            "before_sha256": digest(b"before"),
            "after_sha256": digest(b"after"),
            **extra,
        },
    )
    return assistant, operation


def test_recover_file_after_hash_without_reapplying(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(b"after")
    with SessionStore(tmp_path, tmp_path / "state") as store:
        started_file(store)
        db = store.path
    before = path.read_bytes(), path.stat().st_mtime_ns
    with SessionStore.open(db) as store:
        report = store.reconcile()
        assert report["requires_attention"] is False
        result = store.load_results()[0]
        assert result.status == "ok"
        assert result.data["recovered_from_state"] is True
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        first = store.load_messages(), store.load_events()
        assert store.reconcile()["operations"] == []
        assert (store.load_messages(), store.load_events()) == first


@pytest.mark.parametrize(
    "content,code", [(b"before", "RECOVERY_NOT_EXECUTED"), (b"user edit", "RECOVERY_FILE_CONFLICT")]
)
def test_recover_before_hash_or_conflict_preserves_file_and_requires_attention(
    tmp_path: Path, content: bytes, code: str
) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(content)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        started_file(store)
        report = store.reconcile()
        assert report["requires_attention"] is True
        assert store.load_results()[0].data["code"] == code
        assert store.load_results()[0].status != "ok"
        assert path.read_bytes() == content
        # Reopening a previously reconciled unknown must not bypass attention.
        assert store.reconcile()["requires_attention"] is True


def test_started_command_is_unknown_and_never_executed(tmp_path: Path) -> None:
    sentinel = tmp_path / "side-effect"
    with SessionStore(tmp_path, tmp_path / "state") as store:
        assistant, operation = planned(store, "run_command")
        store.record_approved(
            assistant, {"operation_id": operation, "call_id": "call", "decision": "allow"}
        )
        store.record_intent(
            assistant,
            {
                "operation_id": operation,
                "call_id": "call",
                "tool": "run_command",
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; Path('side-effect').touch()",
                ],
            },
        )
        report = store.reconcile()
        assert report["requires_attention"] is True
        result = store.load_results()[0]
        assert result.status == "unknown"
        assert result.data["intent"]["argv"][0] == sys.executable
        assert not sentinel.exists()
        assert store.reconcile()["requires_attention"] is True


def test_partial_tool_round_gets_one_result_per_call(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        calls = tuple(ToolCall(str(index), "read_file", "{}") for index in range(3))
        assistant = store.record_assistant(Message("assistant", tool_calls=calls))
        first_op = store.operation_id_for(assistant, "0")
        store.record_approved(
            assistant, {"operation_id": first_op, "call_id": "0", "decision": "allow"}
        )
        store.record_intent(assistant, {"operation_id": first_op, "call_id": "0"})
        completed = ToolResult("0", first_op, "read_file", "ok", "done")
        store.record_result(assistant, completed)
        second_op = store.operation_id_for(assistant, "1")
        store.record_approved(
            assistant, {"operation_id": second_op, "call_id": "1", "decision": "allow"}
        )
        report = store.reconcile()
        assert len(report["operations"]) == 2
        results = store.load_results()
        assert len(results) == 3 and results[0] == completed
        assert [item.status for item in results] == ["ok", "cancelled", "cancelled"]
        tool_messages = [message for message in store.load_messages() if message.role == "tool"]
        assert {message.tool_call_id for message in tool_messages} == {"0", "1", "2"}
        assert report["requires_attention"] is False


@pytest.mark.parametrize("kind", ["missing", "directory", "mode"])
def test_missing_changed_type_or_mode_is_explicit(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "value.txt"
    extra: dict[str, object] = {}
    if kind == "directory":
        path.mkdir()
    elif kind == "mode":
        path.write_bytes(b"after")
        extra["mode"] = 0
    with SessionStore(tmp_path, tmp_path / "state") as store:
        started_file(store, **extra)
        report = store.reconcile()
        assert report["requires_attention"] is True
        result = store.load_results()[0]
        assert result.status == "unknown"
        assert result.data["actual_state"] in {
            "missing",
            "not_file",
            "permissions_changed",
            "unavailable",
        }


def test_new_file_missing_is_not_claimed_success(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        started_file(store, "create_file", before_sha256=None)
        store.reconcile()
        assert store.load_results()[0].status == "blocked"
        assert not (tmp_path / "value.txt").exists()


def test_real_crash_after_file_write_before_result_commit(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(b"before")
    script = r"""
import hashlib, json, os, sys
from pathlib import Path
from stellarweaver.domain import Message, ToolCall
from stellarweaver.session.store import SessionStore
root = Path(sys.argv[1])
store = SessionStore(root, root / "state")
assistant = store.record_assistant(Message("assistant", tool_calls=(ToolCall("call", "edit_file", '{}'),)))
operation = store.operation_id_for(assistant, "call")
store.record_approved(assistant, {"operation_id": operation, "call_id": "call", "decision": "allow"})
store.record_intent(assistant, {
    "operation_id": operation, "call_id": "call", "tool": "edit_file", "path": "value.txt",
    "before_sha256": hashlib.sha256(b"before").hexdigest(),
    "after_sha256": hashlib.sha256(b"after").hexdigest(),
})
(root / "session-path.txt").write_text(str(store.path), encoding="utf-8")
(root / "value.txt").write_bytes(b"after")
os._exit(91)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 91, result.stderr
    database = Path((tmp_path / "session-path.txt").read_text(encoding="utf-8"))
    with SessionStore.open(database) as store:
        report = store.reconcile()
        assert report["operations"][0]["status"] == "ok"
        assert store.load_results()[0].data["recovered_from_state"] is True
        assert path.read_bytes() == b"after"


def test_replay_snapshot_neither_recovers_nor_changes_workspace(tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_bytes(b"after")
    with SessionStore(tmp_path, tmp_path / "state") as store:
        started_file(store)
        db = store.path
    before = {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()}
    snapshot = SessionStore.inspect(db)
    assert snapshot.results == ()
    assert snapshot.tool_executions[0]["state"] == "started"
    assert {item: item.read_bytes() for item in tmp_path.rglob("*") if item.is_file()} == before


def test_denied_authorization_never_starts_and_survives_interruption(tmp_path: Path) -> None:
    from stellarweaver.session.store import SessionError

    with SessionStore(tmp_path, tmp_path / "state") as store:
        assistant, operation = planned(store, "run_command")
        payload = {
            "operation_id": operation,
            "call_id": "call",
            "decision": "deny",
            "reason": "not authorized",
        }
        store.record_approved(assistant, payload)
        with pytest.raises(SessionError, match="denied"):
            store.record_intent(assistant, payload)
        report = store.reconcile()
        assert report["requires_attention"] is False
        assert store.load_results()[0].status == "denied"
        assert store.load_results()[0].content == "not authorized"
