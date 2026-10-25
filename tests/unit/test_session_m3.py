"""Persistent boundaries: migration, writer ownership, and immutable inspection."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from stellarweaver.domain import Message, ToolCall, ToolResult
from stellarweaver.session.store import (
    SCHEMA_VERSION,
    SessionBusy,
    SessionError,
    SessionStore,
    SessionWorkspaceMismatch,
    list_sessions,
)


def call(name: str = "read_file", identifier: str = "same") -> ToolCall:
    return ToolCall(identifier, name, '{"path":"value.txt"}')


def start(store: SessionStore, tool: ToolCall, **metadata: object) -> tuple[str, str]:
    assistant = store.record_assistant(Message("assistant", tool_calls=(tool,)))
    operation = store.operation_id_for(assistant, tool.call_id)
    payload = {"operation_id": operation, "call_id": tool.call_id, "tool": tool.name, **metadata}
    store.record_approved(assistant, {**payload, "decision": "allow"})
    store.record_intent(assistant, payload)
    return assistant, operation


def test_round_is_atomic_and_results_are_idempotent(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        tool = call()
        assistant, operation = start(store, tool)
        result = ToolResult(tool.call_id, operation, tool.name, "ok", "saved")
        event = store.record_result(assistant, result)
        before = store.load_messages(), store.load_events()
        assert store.record_result(assistant, result) == event
        assert (store.load_messages(), store.load_events()) == before
        assert len([message for message in store.load_messages() if message.role == "tool"]) == 1
        with pytest.raises(SessionError, match="cannot be overwritten"):
            store.record_result(assistant, replace(result, content="different"))
        with pytest.raises(SessionError, match="Cannot start"):
            store.record_intent(assistant, {"operation_id": operation, "call_id": tool.call_id})
        assert (
            store.connection.execute("SELECT state FROM tool_executions").fetchone()[0]
            == "succeeded"
        )


def test_assistant_duplicate_call_id_rolls_back_all_plans(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        with pytest.raises(SessionError, match="unique"):
            store.record_assistant(Message("assistant", tool_calls=(call(), call())))
        assert store.load_messages() == ()
        assert store.connection.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 0


def test_call_ids_can_repeat_between_assistant_turns(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        a, op1 = start(store, call())
        b, op2 = start(store, call())
        assert a != b and op1 != op2
        for assistant, operation in ((a, op1), (b, op2)):
            store.record_result(assistant, ToolResult("same", operation, "read_file", "ok", "ok"))
        assert len(store.load_results()) == 2


def test_success_without_started_intent_is_rejected(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        assistant = store.record_assistant(Message("assistant", tool_calls=(call(),)))
        op = store.operation_id_for(assistant, "same")
        with pytest.raises(SessionError, match="started"):
            store.record_result(assistant, ToolResult("same", op, "read_file", "ok", "fake"))
        assert store.load_results() == ()


def test_inspection_is_readonly_and_can_read_writer_wal(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        store.append_message(Message("user", "remember me"))
        store.append_event("observed", {"value": 1})
        before = store.path.stat().st_mtime_ns
        snapshot = SessionStore.inspect(store.path)
        assert snapshot.messages[-1].content == "remember me"
        assert snapshot.events[-1].type == "observed"
        assert store.path.stat().st_mtime_ns == before
        assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert snapshot.as_dict()["metadata"]["session_id"] == store.session_id
        assert (
            list_sessions(tmp_path / "state", workspace=tmp_path)[0]["session_id"]
            == store.session_id
        )


def test_workspace_mismatch_releases_writer_lock(tmp_path: Path) -> None:
    data_dir = tmp_path / "state"
    with SessionStore(tmp_path, data_dir) as store:
        sid = store.session_id
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SessionWorkspaceMismatch):
        SessionStore.open(sid, workspace=other, data_dir=data_dir)
    with SessionStore.open(sid, workspace=tmp_path, data_dir=data_dir) as store:
        assert store.workspace == tmp_path.resolve()
        store.begin_run()


def test_real_process_single_writer_and_release(tmp_path: Path) -> None:
    script = """
import sys
from stellarweaver.session.store import SessionStore, SessionBusy
try:
    with SessionStore.open(sys.argv[1]):
        print('acquired')
except SessionBusy:
    print('busy')
"""
    with SessionStore(tmp_path, tmp_path / "state") as store:
        path = store.path
        command = [sys.executable, "-c", script, str(path)]
        result = subprocess.run(command, text=True, capture_output=True, timeout=15, check=True)
        assert result.stdout.strip() == "busy"
        with pytest.raises(SessionBusy):
            SessionStore.open(path)
    result = subprocess.run(command, text=True, capture_output=True, timeout=15, check=True)
    assert result.stdout.strip() == "acquired"


def legacy_session(directory: Path, workspace: Path) -> Path:
    directory.mkdir()
    path = directory / "session.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
CREATE TABLE runs(run_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,workspace TEXT NOT NULL,outcome_json TEXT);
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,event_json TEXT NOT NULL);
CREATE TABLE messages(message_id TEXT PRIMARY KEY,position INTEGER NOT NULL UNIQUE,body_json TEXT NOT NULL);
CREATE TABLE tool_executions(operation_id TEXT PRIMARY KEY,assistant_message_id TEXT NOT NULL,call_id TEXT NOT NULL,tool_name TEXT NOT NULL,state TEXT NOT NULL,intent_json TEXT,result_json TEXT,UNIQUE(assistant_message_id,call_id));
PRAGMA user_version=1;
""")
    connection.execute("INSERT INTO runs VALUES(?,?,?,NULL)", ("run", "legacy", str(workspace)))
    connection.execute(
        "INSERT INTO messages VALUES(?,?,?)",
        ("message", 0, json.dumps(asdict(Message("user", "old text")))),
    )
    connection.commit()
    connection.close()
    return path


def test_v1_inspect_does_not_migrate_then_writer_migrates_without_loss(tmp_path: Path) -> None:
    path = legacy_session(tmp_path / "legacy", tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = SessionStore.inspect(path)
    assert snapshot.schema_version == 1 and snapshot.messages[0].content == "old text"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not (path.parent / "writer.lock").exists()
    with SessionStore.open(path) as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert store.load_messages()[0].content == "old text"
        store.append_message(Message("user", "new"))
        assert store.load_messages()[1].content == "new"
    assert SessionStore.inspect(path).schema_version == SCHEMA_VERSION


def test_checkpoint_round_trip_and_invalid_sequence(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        assert store.read_checkpoint() is None
        event = store.append_event("example", {})
        identifier = store.write_checkpoint({"summary": "derived evidence"}, event.seq)
        checkpoint = store.read_checkpoint()
        assert checkpoint is not None
        assert checkpoint["checkpoint_id"] == identifier
        assert checkpoint["summary"] == {"summary": "derived evidence"}
        with pytest.raises(SessionError, match="unavailable"):
            store.write_checkpoint({}, 1000)
        assert SessionStore.inspect(store.path).checkpoints[-1]["checkpoint_id"] == identifier


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem permission semantics")
def test_private_session_permissions(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        assert store.directory.stat().st_mode & 0o077 == 0
        assert store.path.stat().st_mode & 0o077 == 0


def test_configuration_snapshot_rejects_credentials_and_strips_endpoint_secrets(
    tmp_path: Path,
) -> None:
    from pydantic import ValidationError

    from stellarweaver.config import AppConfig

    with SessionStore(tmp_path, tmp_path / "state") as store:
        public = AppConfig().model_dump()
        public["model"]["base_url"] = "https://user:secret@example.com/v1?api_key=secret#token"
        store.set_config(public)
        snapshot = SessionStore.inspect(store.path)
        config = json.loads(snapshot.metadata["config_json"])
        assert config["model"]["base_url"] == "https://example.com/v1"
        assert config["model"]["api_key_env"] == "STELLARWEAVER_API_KEY"
        assert "secret" not in snapshot.metadata["config_json"]
        with pytest.raises(ValidationError):
            store.set_config({"api_key": "never persist"})


def test_v1_migration_failure_is_atomic_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stellarweaver.session.store as module

    path = legacy_session(tmp_path / "legacy", tmp_path)
    original = module._message

    def fail(body: str) -> Message:
        raise ValueError("injected migration interruption")

    monkeypatch.setattr(module, "_message", fail)
    with pytest.raises(ValueError, match="injected"):
        SessionStore.open(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='sessions'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT body_json FROM messages").fetchone()[0]
    finally:
        connection.close()
    monkeypatch.setattr(module, "_message", original)
    with SessionStore.open(path) as store:
        assert store.load_messages()[0].content == "old text"


def test_record_result_failure_rolls_back_message_result_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        assistant, operation = start(store, call())
        before = store.load_messages(), store.load_events()

        def fail(result: ToolResult) -> None:
            raise OSError("injected failure before transaction commit")

        monkeypatch.setattr(store, "_register_result_artifacts", fail)
        with pytest.raises(OSError, match="injected"):
            store.record_result(
                assistant, ToolResult("same", operation, "read_file", "ok", "result")
            )
        assert (store.load_messages(), store.load_events()) == before
        assert store.load_results() == ()


def test_started_requires_persisted_approval(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        assistant = store.record_assistant(Message("assistant", tool_calls=(call(),)))
        op = store.operation_id_for(assistant, "same")
        with pytest.raises(SessionError, match="authorization"):
            store.record_intent(assistant, {"operation_id": op, "call_id": "same"})
        assert (
            store.connection.execute("SELECT state FROM tool_executions").fetchone()[0] == "planned"
        )


def test_schema_foreign_keys_reject_orphan_tool_operations(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        with pytest.raises(sqlite3.IntegrityError):
            with store.connection:
                store.connection.execute(
                    "INSERT INTO tool_executions(operation_id,assistant_message_id,call_id,tool_name,state) VALUES(?,?,?,?,?)",
                    ("orphan", "missing-assistant", "call", "read_file", "planned"),
                )
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
