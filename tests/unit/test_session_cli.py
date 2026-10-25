"""Public M3 entry points, including a separate-process no-side-effects replay."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stellarweaver.cli import app
from stellarweaver.domain import AssistantReply, Message, ModelRequest, ToolCall
from stellarweaver.providers.fake import FakeModel
from stellarweaver.session.store import SessionStore

runner = CliRunner()


@pytest.fixture
def saved_session(tmp_path: Path) -> tuple[Path, Path, str, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("answer = 41\n", encoding="utf-8")
    data_dir = tmp_path / "state"
    script = tmp_path / "replies.json"
    digest = hashlib.sha256((workspace / "sample.py").read_bytes()).hexdigest()
    argv = [sys.executable, "-c", "from sample import answer; assert answer == 42"]
    grants = tmp_path / "grants.json"
    grants.write_text(
        json.dumps(
            {
                "workspace": str(workspace),
                "grants": [{"tool": "run_command", "argv": argv, "cwd": "."}],
            }
        )
    )
    calls = [
        ("read", "read_file", {"path": "sample.py"}),
        (
            "edit",
            "edit_file",
            {"path": "sample.py", "expected_sha256": digest, "old_text": "41", "new_text": "42"},
        ),
        ("verify", "run_command", {"argv": argv}),
    ]
    replies = [
        {
            "tool_calls": [
                {"call_id": call_id, "name": name, "arguments_json": json.dumps(arguments)}
            ],
            "stop_reason": "tool_calls",
        }
        for call_id, name, arguments in calls
    ]
    replies.append({"text": "The actual check passed."})
    script.write_text(json.dumps(replies))
    result = runner.invoke(
        app,
        [
            "exec",
            "repair the sample",
            "-w",
            str(workspace),
            "--provider",
            "fake",
            "--script",
            str(script),
            "--policy",
            "workspace-edit",
            "--policy-file",
            str(grants),
            "--data-dir",
            str(data_dir),
            "--json",
            "--fail-on-verification-error",
        ],
    )
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output.splitlines()[-1])["payload"]
    assert outcome["verification_status"] == "passed"
    assert (workspace / "sample.py").read_text() == "answer = 42\n"
    return workspace, data_dir, outcome["session_id"], Path(outcome["state_path"])


def hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_configuration_snapshot_records_cli_overrides_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "stellarweaver.toml").write_text(
        '[model]\nprovider = "deepseek"\nsupports_streaming = false\n'
    )
    secret = "private-test-key-must-not-persist"
    monkeypatch.setenv("STELLARWEAVER_API_KEY", secret)
    result = runner.invoke(
        app,
        [
            "exec",
            "offline snapshot",
            "-w",
            str(tmp_path),
            "--provider",
            "fake",
            "--policy",
            "read-only",
            "--stream",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    outcome = json.loads(result.output.splitlines()[-1])["payload"]
    snapshot = SessionStore.inspect(outcome["state_path"])
    public = json.loads(snapshot.metadata["config_json"])
    assert public["model"]["provider"] == "fake"
    assert public["model"]["supports_streaming"] is True
    assert public["policy"]["preset"] == "read-only"
    assert secret not in json.dumps(snapshot.as_dict())
    assert secret.encode() not in Path(outcome["state_path"]).read_bytes()


def test_replay_in_separate_process_never_calls_models_tools_or_changes_state(
    saved_session: tuple[Path, Path, str, Path],
) -> None:
    workspace, data_dir, session_id, state = saved_session
    before_files = hashes(workspace)
    before_db = state.read_bytes()
    source = """
import sys
import stellarweaver.cli as cli
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
def forbidden(*args, **kwargs):
    raise AssertionError('Replay attempted a model, tool, migration or recovery action')
cli.build_provider = forbidden
ToolRegistry.dispatch = forbidden
SessionStore.open = forbidden
SessionStore.reconcile = forbidden
sys.argv = ['stellarweaver', 'replay', sys.argv[1], '--data-dir', sys.argv[2], '--json']
cli.app()
"""
    result = subprocess.run(
        [sys.executable, "-c", source, session_id, str(data_dir)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records[0]["record_type"] == "session"
    assert all(record["source"]["mode"] == "historical" for record in records)
    assert all(record["source"]["session_id"] == session_id for record in records)
    assert {record["record_type"] for record in records} >= {
        "message",
        "event",
        "tool_execution",
        "tool_result",
        "run",
    }
    assert hashes(workspace) == before_files
    assert state.read_bytes() == before_db


@pytest.mark.parametrize("format", ["markdown", "jsonl"])
def test_export_preserves_real_operation_evidence_and_refuses_overwrite(
    saved_session: tuple[Path, Path, str, Path],
    tmp_path: Path,
    format: str,
) -> None:
    workspace, data_dir, session_id, _ = saved_session
    destination = tmp_path / f"export.{format}"
    result = runner.invoke(
        app,
        [
            "export",
            session_id,
            "--data-dir",
            str(data_dir),
            "--format",
            format,
            "--output",
            str(destination),
        ],
    )
    assert result.exit_code == 0, result.output
    content = destination.read_text(encoding="utf-8")
    for evidence in (
        session_id,
        "historical",
        "repair the sample",
        "edit_file",
        "run_command",
        "operation_id",
        "sample.py",
        "exit_code",
        "42",
    ):
        assert evidence in content
    if format == "jsonl":
        records = [json.loads(line) for line in content.splitlines()]
        messages = [record["payload"] for record in records if record["record_type"] == "message"]
        assert any(message["role"] == "assistant" and message["tool_calls"] for message in messages)
        assert any(message["role"] == "tool" and message["tool_call_id"] for message in messages)
    original = destination.read_bytes()
    second = runner.invoke(
        app,
        ["export", session_id, "--data-dir", str(data_dir), "--output", str(destination)],
    )
    assert second.exit_code == 2
    assert destination.read_bytes() == original
    assert (workspace / "sample.py").read_text() == "answer = 42\n"


def test_session_listing_and_unknown_ids(
    saved_session: tuple[Path, Path, str, Path],
    tmp_path: Path,
) -> None:
    workspace, data_dir, session_id, _ = saved_session
    result = runner.invoke(app, ["sessions", "list", "--data-dir", str(data_dir), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["session_id"] == session_id
    other = tmp_path / "other"
    other.mkdir()
    filtered = runner.invoke(
        app,
        ["sessions", "list", "--data-dir", str(data_dir), "--workspace", str(other), "--json"],
    )
    assert filtered.exit_code == 0
    assert json.loads(filtered.output) == []
    replay = runner.invoke(app, ["replay", "missing", "--data-dir", str(data_dir)])
    assert replay.exit_code == 2
    assert not (data_dir / "missing").exists()
    mismatch = runner.invoke(
        app,
        ["replay", session_id, "--data-dir", str(data_dir), "--workspace", str(other)],
    )
    assert mismatch.exit_code == 2
    assert workspace.exists()


def test_resume_new_task_uses_history_without_rerunning_tools(
    saved_session: tuple[Path, Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, data_dir, session_id, state = saved_session
    before = hashes(workspace)

    def reply(request: ModelRequest) -> AssistantReply:
        assert request.messages[-1].content == "Explain the saved result"
        assert any(
            message.role == "tool" and message.tool_call_id == "verify"
            for message in request.messages
        )
        return AssistantReply("The recorded command passed; no new check was run.")

    model = FakeModel(callback=reply)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        [
            "resume",
            session_id,
            "--data-dir",
            str(data_dir),
            "--prompt",
            "Explain the saved result",
            "--provider",
            "fake",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(model.requests) == 1
    outcome = json.loads(result.output.splitlines()[-1])["payload"]
    assert outcome["session_id"] == session_id
    assert outcome["tool_calls"] == 0
    assert hashes(workspace) == before
    assert Path(outcome["state_path"]) == state


def test_resume_wrong_workspace_is_rejected_before_provider_creation(
    saved_session: tuple[Path, Path, str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, data_dir, session_id, _ = saved_session
    other = tmp_path / "other"
    other.mkdir()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Workspace mismatch must be detected before provider creation")

    monkeypatch.setattr("stellarweaver.cli.build_provider", forbidden)
    result = runner.invoke(
        app,
        ["resume", session_id, "--data-dir", str(data_dir), "--workspace", str(other)],
    )
    assert result.exit_code == 2, result.output


def test_resume_does_not_inherit_previous_write_permission_without_tty(
    saved_session: tuple[Path, Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, data_dir, session_id, _ = saved_session
    args = {
        "path": "sample.py",
        "expected_sha256": hashlib.sha256((workspace / "sample.py").read_bytes()).hexdigest(),
        "old_text": "42",
        "new_text": "43",
    }
    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(ToolCall("ungranted", "edit_file", json.dumps(args)),),
                stop_reason="tool_calls",
            ),
        ]
    )
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        [
            "resume",
            session_id,
            "--data-dir",
            str(data_dir),
            "--prompt",
            "Change the answer",
            "--provider",
            "fake",
            "--json",
        ],
    )
    assert result.exit_code == 3, result.output
    assert (workspace / "sample.py").read_text() == "answer = 42\n"
    outcome = json.loads(result.output.splitlines()[-1])["payload"]
    assert outcome["run_status"] == "blocked"
    assert len(model.requests) == 1


def test_resume_busy_writer_returns_blocked(
    saved_session: tuple[Path, Path, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, data_dir, session_id, _ = saved_session

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Busy session must be rejected before provider creation")

    monkeypatch.setattr("stellarweaver.cli.build_provider", forbidden)
    with SessionStore.open(session_id, workspace=workspace, data_dir=data_dir):
        result = runner.invoke(app, ["resume", session_id, "--data-dir", str(data_dir)])
    assert result.exit_code == 3, result.output
    assert "in use" in result.output


def test_unknown_started_command_blocks_resume_and_is_never_reexecuted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    marker = workspace / "must-not-be-created"
    argv = [sys.executable, "-c", "from pathlib import Path; Path('must-not-be-created').touch()"]
    call = ToolCall("uncertain-command", "run_command", json.dumps({"argv": argv}))
    data_dir = tmp_path / "state"
    with SessionStore(workspace, data_dir=data_dir) as store:
        session_id = store.session_id
        store.append_message(Message("system", "A saved coding task."))
        store.append_message(Message("user", "Run the verification."))
        assistant = store.record_assistant(Message("assistant", tool_calls=(call,)))
        operation = store.operation_id_for(assistant, call.call_id)
        store.record_approved(
            assistant, {"operation_id": operation, "call_id": call.call_id, "decision": "allow"}
        )
        store.record_intent(
            assistant,
            {
                "operation_id": operation,
                "call_id": call.call_id,
                "tool_name": call.name,
                "argv": argv,
                "cwd": ".",
            },
        )

    model = FakeModel([AssistantReply("Awaiting a new explicit task.")])
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        ["resume", session_id, "--data-dir", str(data_dir), "--provider", "fake", "--json"],
    )
    assert result.exit_code == 3, result.output
    assert not model.requests
    assert not marker.exists()
    snapshot = SessionStore.inspect(session_id, data_dir=data_dir)
    assert snapshot.tool_results[-1].status == "unknown"
    assert snapshot.tool_results[-1].data["code"] == "RECOVERY_COMMAND_UNKNOWN"


@pytest.mark.parametrize("corruption", ["database", "message"])
def test_replay_reports_malformed_sessions_as_a_controlled_error(
    tmp_path: Path,
    corruption: str,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    data_dir = tmp_path / "state"
    with SessionStore(workspace, data_dir=data_dir) as store:
        session_id, path = store.session_id, store.path
        if corruption == "message":
            store.connection.execute(
                "INSERT INTO messages VALUES(?,?,?)", ("bad", 0, '["private-malformed-content"]')
            )
            store.connection.commit()
    if corruption == "database":
        path.write_bytes(b"private-malformed-content: not sqlite")
    result = runner.invoke(app, ["replay", session_id, "--data-dir", str(data_dir), "--json"])
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert "private-malformed-content" not in result.output


@pytest.mark.parametrize("command", ["resume", "replay", "export"])
def test_m3_entrypoints_have_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert "--data-dir" in result.output
