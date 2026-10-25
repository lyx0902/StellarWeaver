"""Short-transaction SQLite journal, crash reconciliation, and read-only snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from uuid import uuid4

from platformdirs import user_data_path

from stellarweaver.domain import AgentEvent, JSONDict, Message, ToolCall, ToolResult, ToolStatus
from stellarweaver.session.locking import (
    SessionBusy,
    SessionError,
    SessionNotFound,
    SessionWorkspaceMismatch,
    WriterLock,
    workspace_write_lock,
)

__all__ = [
    "SCHEMA_VERSION",
    "SessionBusy",
    "SessionError",
    "SessionNotFound",
    "SessionSnapshot",
    "SessionStore",
    "SessionWorkspaceMismatch",
    "list_sessions",
]
SCHEMA_VERSION = 2
FINAL_STATES = frozenset({"succeeded", "failed", "denied", "cancelled", "unknown"})
RESULT_STATES = {"ok": "succeeded", "error": "failed", "blocked": "denied"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _message(body: str) -> Message:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise SessionError("Malformed message record")
    data["tool_calls"] = tuple(ToolCall(**call) for call in data.get("tool_calls", []))
    return Message(**data)


def _result(body: str) -> ToolResult:
    data = json.loads(body)
    if not isinstance(data, dict):
        raise SessionError("Malformed tool result record")
    data["artifact_ids"] = tuple(data.get("artifact_ids", []))
    return ToolResult(**data)


def _root(data_dir: Path | None) -> Path:
    return (
        (data_dir if data_dir is not None else user_data_path("stellarweaver") / "sessions")
        .expanduser()
        .resolve()
    )


def _database(session: str | Path, data_dir: Path | None) -> Path:
    value = str(session)
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        path = _root(data_dir) / value / "session.sqlite3"
    else:
        path = Path(session).expanduser().absolute()
        if path.is_dir():
            path /= "session.sqlite3"
    if path.name != "session.sqlite3" or not path.is_file():
        raise SessionNotFound(f"Session database not found: {path}")
    if path.is_symlink() or path.parent.is_symlink():
        raise SessionError("Session symlinks are not supported")
    return path.resolve(strict=True)


class _SnapshotConnection(sqlite3.Connection):
    """A disposable WAL-aware copy never creates sidecars beside the source DB."""

    temporary: tempfile.TemporaryDirectory[str] | None = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self.temporary is not None:
                self.temporary.cleanup()
                self.temporary = None


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_size, info.st_mtime_ns


def _readonly(path: Path) -> sqlite3.Connection:
    # SQLite mode=ro can still create/change source -wal/-shm files. Copy a stable
    # database+WAL pair privately, then let SQLite read/recover only that copy.
    temporary = tempfile.TemporaryDirectory(prefix="stellarweaver-inspect-")
    copied = Path(temporary.name) / "session.sqlite3"
    wal = path.with_name(path.name + "-wal")
    copied_wal = copied.with_name(copied.name + "-wal")
    try:
        for _ in range(5):
            before = (_fingerprint(path), _fingerprint(wal))
            try:
                shutil.copyfile(path, copied)
                if before[1] is not None:
                    shutil.copyfile(wal, copied_wal)
                else:
                    copied_wal.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            if before == (_fingerprint(path), _fingerprint(wal)):
                break
        else:
            raise SessionBusy("Session changes too quickly for a consistent read-only snapshot")
        connection = sqlite3.connect(
            copied.as_uri() + "?mode=ro", uri=True, timeout=1, factory=_SnapshotConnection
        )
        connection.temporary = temporary
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except BaseException:
        temporary.cleanup()
        raise


def _metadata(connection: sqlite3.Connection, path: Path) -> JSONDict:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in {1, SCHEMA_VERSION}:
        raise SessionError(f"Unsupported session schema version: {version}")
    if version == 1:
        rows = connection.execute("SELECT DISTINCT session_id,workspace FROM runs").fetchall()
        if len(rows) != 1:
            raise SessionError("Legacy session has ambiguous workspace metadata")
        return {
            "session_id": rows[0][0],
            "workspace": rows[0][1],
            "status": "legacy",
            "created_at": "",
            "updated_at": "",
            "schema_version": version,
            "path": str(path),
        }
    row = connection.execute("SELECT * FROM sessions").fetchone()
    if row is None:
        raise SessionError("Session metadata is missing")
    result = dict(row)
    result["workspace"] = result.pop("workspace_root")
    result["path"] = str(path)
    return result


def _verify_workspace(metadata: JSONDict, workspace: Path | None) -> Path:
    stored = Path(metadata["workspace"]).resolve(strict=False)
    if workspace is not None and workspace.expanduser().resolve(strict=True) != stored:
        raise SessionWorkspaceMismatch(f"Session workspace is {stored}, not {workspace}")
    return stored


@dataclass(frozen=True)
class SessionSnapshot:
    path: Path
    metadata: JSONDict
    messages: tuple[Message, ...]
    events: tuple[AgentEvent, ...]
    results: tuple[ToolResult, ...]
    tool_executions: tuple[JSONDict, ...]
    runs: tuple[JSONDict, ...]
    checkpoints: tuple[JSONDict, ...] = ()

    @property
    def session_id(self) -> str:
        return str(self.metadata["session_id"])

    @property
    def workspace(self) -> Path:
        return Path(self.metadata["workspace"])

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def status(self) -> str:
        return str(self.metadata["status"])

    @property
    def schema_version(self) -> int:
        return int(self.metadata["schema_version"])

    @property
    def tool_calls(self) -> tuple[JSONDict, ...]:
        return self.tool_executions

    @property
    def tool_results(self) -> tuple[ToolResult, ...]:
        return self.results

    def as_dict(self) -> JSONDict:
        return {
            "metadata": self.metadata,
            "messages": [asdict(item) for item in self.messages],
            "events": [asdict(item) for item in self.events],
            "tool_executions": list(self.tool_executions),
            "tool_results": [asdict(item) for item in self.results],
            "runs": list(self.runs),
            "checkpoints": list(self.checkpoints),
        }


def list_sessions(data_dir: Path | None = None, workspace: Path | None = None) -> list[JSONDict]:
    root = _root(data_dir)
    if not root.is_dir():
        return []
    wanted = workspace.expanduser().resolve(strict=True) if workspace is not None else None
    records: list[JSONDict] = []
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        path = child / "session.sqlite3"
        if path.is_symlink() or not path.is_file():
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = _readonly(path)
            metadata = _metadata(connection, path)
            if wanted is None or Path(metadata["workspace"]).resolve(strict=False) == wanted:
                records.append(metadata)
        except (OSError, sqlite3.Error, SessionError, ValueError):
            continue
        finally:
            if connection is not None:
                connection.close()
    return sorted(records, key=lambda item: (item["updated_at"], item["session_id"]), reverse=True)


class SessionStore:
    def __init__(self, workspace: Path, data_dir: Path | None = None) -> None:
        self.workspace = workspace.expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise SessionError("Workspace must be a directory")
        self.session_id = uuid4().hex
        self.run_id = uuid4().hex
        self.directory = _root(data_dir) / self.session_id
        self.directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.path = self.directory / "session.sqlite3"
        self.artifacts_dir = self.directory / "artifacts"
        self.artifacts_dir.mkdir(mode=0o700)
        self._lock = WriterLock(self.directory / "writer.lock")
        self._lock.acquire()
        self._closed = False
        try:
            self.connection = sqlite3.connect(self.path)
            self.connection.row_factory = sqlite3.Row
            self._create()
            self._migrate()
            if os.name != "nt":
                self.path.chmod(0o600)
        except BaseException:
            if hasattr(self, "connection"):
                self.connection.close()
            self._lock.release()
            raise

    @classmethod
    def open(
        cls,
        session_id_or_path: str | Path,
        workspace: Path | None = None,
        data_dir: Path | None = None,
    ) -> SessionStore:
        path = _database(session_id_or_path, data_dir)
        lock = WriterLock(path.parent / "writer.lock")
        lock.acquire()
        instance = cls.__new__(cls)
        instance._lock = lock
        instance._closed = False
        try:
            instance.connection = sqlite3.connect(path, timeout=1)
            instance.connection.row_factory = sqlite3.Row
            metadata = _metadata(instance.connection, path)
            instance.workspace = _verify_workspace(metadata, workspace)
            if not instance.workspace.is_dir():
                raise SessionWorkspaceMismatch(
                    f"Session workspace is unavailable: {instance.workspace}"
                )
            instance.session_id = str(metadata["session_id"])
            row = instance.connection.execute(
                "SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            instance.run_id = str(row[0]) if row is not None else uuid4().hex
            instance.path = path
            instance.directory = path.parent
            instance.artifacts_dir = instance.directory / "artifacts"
            instance.artifacts_dir.mkdir(exist_ok=True, mode=0o700)
            instance._migrate()
            return instance
        except BaseException as exc:
            if hasattr(instance, "connection"):
                instance.connection.close()
            lock.release()
            if isinstance(exc, sqlite3.Error):
                raise SessionError("Malformed or unreadable session database") from exc
            raise

    @classmethod
    def inspect(
        cls,
        session_id_or_path: str | Path,
        workspace: Path | None = None,
        data_dir: Path | None = None,
    ) -> SessionSnapshot:
        path = _database(session_id_or_path, data_dir)
        try:
            connection = _readonly(path)
        except sqlite3.Error as exc:
            raise SessionError("Cannot open the session database") from exc
        try:
            connection.execute("BEGIN")
            metadata = _metadata(connection, path)
            _verify_workspace(metadata, workspace)
            messages = tuple(
                _message(row[0])
                for row in connection.execute("SELECT body_json FROM messages ORDER BY position")
            )
            events = tuple(
                AgentEvent(**json.loads(row[0]))
                for row in connection.execute("SELECT event_json FROM events ORDER BY seq")
            )
            operations = tuple(
                dict(row)
                for row in connection.execute("SELECT * FROM tool_executions ORDER BY rowid")
            )
            results = tuple(
                _result(row["result_json"]) for row in operations if row["result_json"] is not None
            )
            runs = tuple(
                dict(row) for row in connection.execute("SELECT * FROM runs ORDER BY rowid")
            )
            checkpoints: tuple[JSONDict, ...] = ()
            if metadata["schema_version"] >= 2:
                checkpoints = tuple(
                    cls._checkpoint_row(row)
                    for row in connection.execute("SELECT * FROM checkpoints ORDER BY rowid")
                )
            return SessionSnapshot(
                path, metadata, messages, events, results, operations, runs, checkpoints
            )
        except SessionError:
            raise
        except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise SessionError("Malformed or unreadable session record") from exc
        finally:
            connection.close()

    list_sessions = staticmethod(list_sessions)

    def _create(self) -> None:
        self.connection.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            CREATE TABLE runs (run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, workspace TEXT NOT NULL, outcome_json TEXT);
            CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,event_json TEXT NOT NULL);
            CREATE TABLE messages (message_id TEXT PRIMARY KEY,position INTEGER NOT NULL UNIQUE,body_json TEXT NOT NULL);
            CREATE TABLE tool_executions (operation_id TEXT PRIMARY KEY,assistant_message_id TEXT NOT NULL,call_id TEXT NOT NULL,tool_name TEXT NOT NULL,state TEXT NOT NULL,intent_json TEXT,result_json TEXT,UNIQUE(assistant_message_id,call_id));
            PRAGMA user_version=1;
        """)
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs VALUES(?,?,?,NULL)",
                (self.run_id, self.session_id, str(self.workspace)),
            )

    def _migrate(self) -> None:
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 2:
            return
        if version != 1:
            raise SessionError(f"Unsupported session schema version: {version}")
        with self.connection:
            # Explicit BEGIN keeps DDL transactional under sqlite3 legacy mode.
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                "CREATE TABLE sessions(session_id TEXT PRIMARY KEY,workspace_root TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,status TEXT NOT NULL,config_json TEXT NOT NULL,schema_version INTEGER NOT NULL)"
            )
            self.connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                (
                    self.session_id,
                    str(self.workspace),
                    now(),
                    now(),
                    "active",
                    "{}",
                    SCHEMA_VERSION,
                ),
            )
            for definition in (
                "args_json TEXT NOT NULL DEFAULT '{}'",
                "decision_json TEXT NOT NULL DEFAULT '{}'",
                "started_at TEXT",
                "finished_at TEXT",
                "result_event_seq INTEGER",
            ):
                self.connection.execute("ALTER TABLE tool_executions ADD COLUMN " + definition)
            self.connection.execute("ALTER TABLE tool_executions RENAME TO tool_executions_v1")
            self.connection.execute("""CREATE TABLE tool_executions (
                operation_id TEXT PRIMARY KEY, assistant_message_id TEXT NOT NULL REFERENCES messages(message_id),
                call_id TEXT NOT NULL, tool_name TEXT NOT NULL, state TEXT NOT NULL,
                intent_json TEXT, result_json TEXT, args_json TEXT NOT NULL DEFAULT '{}',
                decision_json TEXT NOT NULL DEFAULT '{}', started_at TEXT, finished_at TEXT,
                result_event_seq INTEGER REFERENCES events(seq), UNIQUE(assistant_message_id,call_id)
            )""")
            self.connection.execute("INSERT INTO tool_executions SELECT * FROM tool_executions_v1")
            self.connection.execute("DROP TABLE tool_executions_v1")
            self.connection.execute(
                "CREATE TABLE checkpoints(checkpoint_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(session_id),through_event_seq INTEGER NOT NULL,summary_json TEXT NOT NULL,created_at TEXT NOT NULL)"
            )
            self.connection.execute(
                "CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(session_id),kind TEXT NOT NULL,relative_storage_path TEXT NOT NULL,sha256 TEXT NOT NULL,byte_length INTEGER NOT NULL,metadata_json TEXT NOT NULL)"
            )
            self.connection.execute("CREATE INDEX execution_state ON tool_executions(state)")
            self.connection.execute(
                "UPDATE tool_executions SET state=CASE state WHEN 'ok' THEN 'succeeded' WHEN 'error' THEN 'failed' WHEN 'blocked' THEN 'denied' ELSE state END"
            )
            for message_row in self.connection.execute(
                "SELECT message_id,body_json FROM messages"
            ).fetchall():
                message = _message(message_row["body_json"])
                for call in message.tool_calls:
                    self.connection.execute(
                        "UPDATE tool_executions SET args_json=? WHERE assistant_message_id=? AND call_id=?",
                        (call.arguments_json, message_row["message_id"], call.call_id),
                    )
            self.connection.execute("PRAGMA user_version=2")

    def begin_run(self) -> str:
        if self._closed:
            raise SessionError("Session store is closed")
        self.run_id = uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs VALUES(?,?,?,NULL)",
                (self.run_id, self.session_id, str(self.workspace)),
            )
            self.connection.execute("UPDATE sessions SET status='active',updated_at=?", (now(),))
        return self.run_id

    def _insert_message(self, message: Message, message_id: str | None = None) -> str:
        message_id = message_id or uuid4().hex
        position = self.connection.execute(
            "SELECT COALESCE(MAX(position),-1)+1 FROM messages"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO messages VALUES(?,?,?)", (message_id, position, _json(asdict(message)))
        )
        return message_id

    def append_message(self, message: Message) -> str:
        if message.role == "assistant":
            return self.record_assistant(message)
        with self.connection:
            return self._insert_message(message)

    def record_assistant(self, message: Message) -> str:
        if message.role != "assistant":
            raise SessionError("record_assistant requires an assistant message")
        if len({call.call_id for call in message.tool_calls}) != len(message.tool_calls):
            raise SessionError("Tool call IDs must be unique within an assistant message")
        with self.connection:
            message_id = self._insert_message(message)
            for call in message.tool_calls:
                self.connection.execute(
                    "INSERT INTO tool_executions(operation_id,assistant_message_id,call_id,tool_name,state,args_json) VALUES(?,?,?,?,?,?)",
                    (
                        uuid4().hex,
                        message_id,
                        call.call_id,
                        call.name,
                        "planned",
                        call.arguments_json,
                    ),
                )
            self._event("assistant.completed", asdict(message))
        return message_id

    def operation_id_for(self, assistant_id: str, call_id: str) -> str:
        row = self.connection.execute(
            "SELECT operation_id FROM tool_executions WHERE assistant_message_id=? AND call_id=?",
            (assistant_id, call_id),
        ).fetchone()
        if row is None:
            raise SessionError("Tool call was not planned in a persisted assistant message")
        return str(row[0])

    def _event(self, kind: str, payload: JSONDict) -> AgentEvent:
        seq = int(
            self.connection.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        )
        event = AgentEvent(
            self.session_id, self.run_id, seq, kind, json.loads(_json(payload)), now()
        )
        self.connection.execute("INSERT INTO events VALUES(?,?)", (seq, _json(asdict(event))))
        self.connection.execute("UPDATE sessions SET updated_at=?", (event.timestamp,))
        self.last_event = event
        return event

    def append_event(self, kind: str, payload: JSONDict) -> AgentEvent:
        with self.connection:
            return self._event(kind, payload)

    def _operation(self, assistant_id: str, operation_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM tool_executions WHERE assistant_message_id=? AND operation_id=?",
            (assistant_id, operation_id),
        ).fetchone()
        if row is None:
            raise SessionError("Operation ID does not match the persisted tool call")
        return cast(sqlite3.Row, row)

    def record_approved(self, assistant_id: str, payload: JSONDict) -> AgentEvent:
        with self.connection:
            row = self._operation(assistant_id, str(payload["operation_id"]))
            if row["state"] not in {"planned", "approved"}:
                raise SessionError(f"Cannot authorize operation in state {row['state']}")
            if row["call_id"] != payload.get("call_id"):
                raise SessionError("Authorization call ID does not match operation")
            self.connection.execute(
                "UPDATE tool_executions SET decision_json=? WHERE operation_id=?",
                (_json(payload), payload["operation_id"]),
            )
            if payload.get("decision") == "allow":
                self.connection.execute(
                    "UPDATE tool_executions SET state='approved',decision_json=? WHERE operation_id=?",
                    (_json(payload), payload["operation_id"]),
                )
            return self._event("tool.authorization", payload)

    def record_intent(self, assistant_id: str, payload: JSONDict) -> AgentEvent:
        with self.connection:
            row = self._operation(assistant_id, str(payload["operation_id"]))
            if row["call_id"] != payload["call_id"]:
                raise SessionError("Journal call ID does not match operation")
            decision = json.loads(row["decision_json"])
            if decision and decision.get("decision") != "allow":
                raise SessionError("Cannot start a denied operation")
            if row["state"] != "approved":
                raise SessionError(
                    f"Cannot start operation in state {row['state']}; authorization is required"
                )
            self.connection.execute(
                "UPDATE tool_executions SET state='started',intent_json=?,started_at=? WHERE operation_id=?",
                (_json(payload), now(), payload["operation_id"]),
            )
            return self._event("tool.started", payload)

    def record_result(self, assistant_id: str, result: ToolResult) -> AgentEvent:
        if result.status not in {"ok", "error", "denied", "cancelled", "blocked", "unknown"}:
            raise SessionError("Invalid tool result status")
        with self.connection:
            row = self._operation(assistant_id, result.operation_id)
            if row["call_id"] != result.call_id or row["tool_name"] != result.tool_name:
                raise SessionError("Tool result does not match its planned call")
            if row["result_json"] is not None:
                if _result(row["result_json"]) != result:
                    raise SessionError("A final tool result cannot be overwritten")
                seq = row["result_event_seq"]
                if seq is not None:
                    event_row = self.connection.execute(
                        "SELECT event_json FROM events WHERE seq=?", (seq,)
                    ).fetchone()
                    return AgentEvent(**json.loads(event_row[0]))
                event = self._event("tool.completed", asdict(result))
                self.connection.execute(
                    "UPDATE tool_executions SET result_event_seq=? WHERE operation_id=?",
                    (event.seq, result.operation_id),
                )
                return event
            if row["state"] in FINAL_STATES:
                raise SessionError("Operation is final without a matching result")
            if result.status == "ok" and row["state"] != "started":
                raise SessionError("A successful operation must have a persisted started intent")
            state = RESULT_STATES.get(result.status, result.status)
            event = self._event("tool.completed", asdict(result))
            self.connection.execute(
                "UPDATE tool_executions SET state=?,result_json=?,finished_at=?,result_event_seq=? WHERE operation_id=?",
                (state, _json(asdict(result)), now(), event.seq, result.operation_id),
            )
            self._insert_message(
                Message("tool", _json(asdict(result)), tool_call_id=result.call_id)
            )
            self._register_result_artifacts(result)
            return event

    def _register_result_artifacts(self, result: ToolResult) -> None:
        for identifier in result.artifact_ids:
            path = Path(identifier)
            if not path.is_absolute():
                path = self.artifacts_dir / path
            resolved = path.resolve(strict=False)
            if (
                not resolved.is_relative_to(self.artifacts_dir.resolve())
                or not resolved.is_file()
                or resolved.is_symlink()
            ):
                continue
            try:
                data = resolved.read_bytes()
            except OSError:
                continue
            relative = resolved.relative_to(self.directory).as_posix()
            self.connection.execute(
                "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?,?)",
                (
                    relative,
                    self.session_id,
                    resolved.suffix.lstrip(".") or "output",
                    relative,
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    _json({"operation_id": result.operation_id}),
                ),
            )

    def load_messages(self) -> tuple[Message, ...]:
        return tuple(
            _message(row[0])
            for row in self.connection.execute("SELECT body_json FROM messages ORDER BY position")
        )

    def load_events(self) -> tuple[AgentEvent, ...]:
        return tuple(
            AgentEvent(**json.loads(row[0]))
            for row in self.connection.execute("SELECT event_json FROM events ORDER BY seq")
        )

    def load_results(self) -> tuple[ToolResult, ...]:
        return tuple(
            _result(row[0])
            for row in self.connection.execute(
                "SELECT result_json FROM tool_executions WHERE result_json IS NOT NULL ORDER BY rowid"
            )
        )

    @staticmethod
    def _checkpoint_row(row: sqlite3.Row) -> JSONDict:
        data = dict(row)
        data["summary"] = json.loads(data.pop("summary_json"))
        return data

    def write_checkpoint(self, summary: JSONDict, through_event_seq: int) -> str:
        maximum = int(
            self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        )
        if not 0 <= through_event_seq <= maximum:
            raise SessionError("Checkpoint references an unavailable event sequence")
        identifier = uuid4().hex
        with self.connection:
            self.connection.execute(
                "INSERT INTO checkpoints VALUES(?,?,?,?,?)",
                (identifier, self.session_id, through_event_seq, _json(summary), now()),
            )
            self._event(
                "context.checkpoint",
                {"checkpoint_id": identifier, "through_event_seq": through_event_seq},
            )
        return identifier

    def read_checkpoint(self) -> JSONDict | None:
        row = self.connection.execute(
            "SELECT * FROM checkpoints ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return None if row is None else self._checkpoint_row(row)

    def set_config(self, snapshot: JSONDict) -> None:
        """Store only typed public settings, never arbitrary credential fields."""
        from urllib.parse import urlsplit, urlunsplit

        from stellarweaver.config import AppConfig

        public = AppConfig.model_validate(snapshot).model_dump(mode="json")
        endpoint = urlsplit(public["model"]["base_url"])
        hostname = endpoint.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        if endpoint.port is not None:
            hostname += f":{endpoint.port}"
        public["model"]["base_url"] = urlunsplit((endpoint.scheme, hostname, endpoint.path, "", ""))
        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET config_json=?,updated_at=?", (_json(public), now())
            )

    def finish(self, outcome: JSONDict) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET outcome_json=? WHERE run_id=?", (_json(outcome), self.run_id)
            )
            self.connection.execute(
                "UPDATE sessions SET status=?,updated_at=?",
                (str(outcome.get("run_status", "completed")), now()),
            )

    def reconcile(self) -> JSONDict:
        """Coordinate tools and undo from evidence, never repeat a filesystem action."""
        with workspace_write_lock(self.workspace):
            report = self._reconcile_tools()
            self._reconcile_undos(report)
            if report["operations"]:
                self.append_event("session.reconciled", report)
            return report

    def _reconcile_tools(self) -> JSONDict:
        """Resolve incomplete tool rounds from evidence, never execute a tool."""
        reports: list[JSONDict] = []
        # V1 persisted assistant messages before creating any planned journal rows.
        with self.connection:
            for row in self.connection.execute(
                "SELECT message_id,body_json FROM messages ORDER BY position"
            ).fetchall():
                message = _message(row["body_json"])
                if message.role != "assistant":
                    continue
                for call in message.tool_calls:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO tool_executions(operation_id,assistant_message_id,call_id,tool_name,state,args_json) VALUES(?,?,?,?,?,?)",
                        (
                            uuid4().hex,
                            row["message_id"],
                            call.call_id,
                            call.name,
                            "planned",
                            call.arguments_json,
                        ),
                    )
        rows = self.connection.execute(
            "SELECT * FROM tool_executions WHERE result_json IS NULL ORDER BY rowid"
        ).fetchall()
        attention = any(
            result.status == "unknown"
            or (
                result.status == "blocked"
                and str(result.data.get("code", "")).startswith("RECOVERY_")
            )
            for result in self.load_results()
        )
        for row in rows:
            state = row["state"]
            status: ToolStatus = "cancelled"
            data: JSONDict = {
                "code": "RECOVERED_NOT_EXECUTED",
                "previous_state": state,
                "executed": False,
                "retryable": False,
            }
            content = "Interrupted before execution; authorization must be checked again."
            decision = json.loads(row["decision_json"] or "{}")
            if state in {"planned", "approved"} and decision.get("decision") in {
                "deny",
                "denied",
                "blocked",
            }:
                status = "blocked" if decision["decision"] == "blocked" else "denied"
                content = str(decision.get("reason", "Authorization did not allow execution."))
                data.update({"code": "PERMISSION_DENIED", "decision": decision["decision"]})
            if state == "started":
                if row["tool_name"] in {"create_file", "edit_file"}:
                    status, content, details = self._reconcile_file(
                        json.loads(row["intent_json"] or "{}")
                    )
                    data.update(details)
                elif row["tool_name"] == "run_command":
                    status = "unknown"
                    content = "Command started without a final result. Its effects are unknown; it was not rerun."
                    data.update(
                        {
                            "code": "RECOVERY_COMMAND_UNKNOWN",
                            "executed": None,
                            "intent": json.loads(row["intent_json"] or "{}"),
                        }
                    )
                else:
                    status = "blocked"
                    content = "Read-only tool interrupted without a saved result; it was not rerun."
                    data["code"] = "RECOVERY_RESULT_UNAVAILABLE"
            needs_attention = status in {"unknown", "blocked"}
            attention = attention or needs_attention
            intent = json.loads(row["intent_json"] or "{}")
            artifacts = tuple(
                str(intent[key])
                for key in ("before_artifact_id", "diff_artifact_id")
                if intent.get(key)
            )
            result = ToolResult(
                row["call_id"],
                row["operation_id"],
                row["tool_name"],
                status,
                content,
                data,
                artifacts,
            )
            self.record_result(row["assistant_message_id"], result)
            reports.append(
                {
                    "operation_id": row["operation_id"],
                    "tool_name": row["tool_name"],
                    "status": status,
                    "content": content,
                    "data": data,
                    "requires_attention": needs_attention,
                }
            )
        report = {
            "session_id": self.session_id,
            "requires_attention": attention,
            "operations": reports,
        }
        return report

    def _reconcile_undos(self, report: JSONDict) -> None:
        pending: dict[str, JSONDict] = {}
        unresolved: dict[str, JSONDict] = {}
        for event in self.load_events():
            if event.type == "changes.undo.started":
                pending[str(event.payload["undo_id"])] = event.payload
            elif event.type == "changes.undo.completed":
                identity = str(event.payload["undo_id"])
                pending.pop(identity, None)
                if event.payload.get("requires_attention"):
                    unresolved[identity] = event.payload
                elif event.payload.get("status") == "ok":
                    target = event.payload.get("target_operation_id")
                    unresolved = {
                        key: value
                        for key, value in unresolved.items()
                        if key != identity and value.get("target_operation_id") != target
                    }
        report["requires_attention"] = bool(report["requires_attention"] or unresolved)
        for intent in pending.values():
            status, content, data = self._inspect_undo_state(intent)
            outcome = {
                **intent,
                **data,
                "status": status,
                "message": content,
                "requires_attention": status != "ok",
            }
            self.append_event("changes.undo.completed", outcome)
            report["operations"].append(
                {
                    "operation_id": intent["undo_id"],
                    "tool_name": "undo",
                    "status": status,
                    "content": content,
                    "data": outcome,
                    "requires_attention": outcome["requires_attention"],
                }
            )
            report["requires_attention"] = bool(
                report["requires_attention"] or outcome["requires_attention"]
            )

    def _inspect_undo_state(self, intent: JSONDict) -> tuple[str, str, JSONDict]:
        from stellarweaver.tools.workspace import MAX_EDIT_BYTES, ToolError, Workspace

        details: JSONDict = {"code": "UNDO_RECOVERY_CONFLICT", "recovered_from_state": False}
        relative, desired, mode = (
            intent.get("path"),
            intent.get("restore_sha256"),
            intent.get("mode"),
        )
        if not isinstance(relative, str) or type(mode) is not int:
            return "unknown", "Interrupted undo lacks path or permission evidence.", details
        if desired is None and intent.get("action") != "delete_created_file":
            return "unknown", "Interrupted undo lacks a validated restore hash.", details
        if desired is not None and (
            not isinstance(desired, str) or not re.fullmatch(r"[0-9a-f]{64}", desired)
        ):
            return "unknown", "Interrupted undo has an invalid restore hash.", details
        try:
            workspace = Workspace(self.workspace)
            workspace.protect(self.directory)
            path = workspace.resolve(relative, must_exist=False)
            raw = self.workspace / relative
            if raw.is_symlink() or any(
                parent.is_symlink()
                for parent in raw.parents
                if parent != self.workspace and parent.is_relative_to(self.workspace)
            ):
                return (
                    "unknown",
                    "Undo target changed to a symlink; preserved without changes.",
                    {**details, "actual_state": "symlink"},
                )
            if not path.exists():
                if desired is None:
                    return (
                        "ok",
                        "Created file is absent; undo recovered from state without repeating deletion.",
                        {
                            "code": "UNDO_RECOVERED_FROM_STATE",
                            "actual_state": "missing",
                            "recovered_from_state": True,
                        },
                    )
                return (
                    "unknown",
                    "Undo target is missing; workspace preserved.",
                    {**details, "actual_state": "missing"},
                )
            if not path.is_file():
                return (
                    "unknown",
                    "Undo target changed type; workspace preserved.",
                    {**details, "actual_state": "not_file"},
                )
            actual_mode = stat.S_IMODE(path.stat().st_mode)
            if actual_mode != mode:
                return (
                    "unknown",
                    "Undo target permissions changed; workspace preserved.",
                    {**details, "actual_state": "permissions_changed", "actual_mode": actual_mode},
                )
            with path.open("rb") as stream:
                contents = stream.read(MAX_EDIT_BYTES + 1)
            if len(contents) > MAX_EDIT_BYTES:
                return (
                    "unknown",
                    "Undo target grew beyond the edit limit; workspace preserved.",
                    {**details, "actual_state": "too_large"},
                )
            actual = hashlib.sha256(contents).hexdigest()
            details["actual_sha256"] = actual
            if actual == desired:
                return (
                    "ok",
                    "Before contents and permissions are present; undo recovered without repeating it.",
                    {**details, "code": "UNDO_RECOVERED_FROM_STATE", "recovered_from_state": True},
                )
            if actual == intent.get("expected_sha256"):
                return (
                    "blocked",
                    "No completed undo was observed; an explicit new task or undo is required.",
                    {**details, "code": "UNDO_RECOVERY_NOT_EXECUTED"},
                )
            return (
                "unknown",
                "Undo target matches neither expected nor restored hash; workspace preserved.",
                details,
            )
        except (OSError, ValueError, ToolError) as exc:
            return (
                "unknown",
                "Undo target could not be safely inspected; workspace preserved.",
                {**details, "actual_state": "unavailable", "error_type": type(exc).__name__},
            )

    def _reconcile_file(self, intent: JSONDict) -> tuple[ToolStatus, str, JSONDict]:
        from stellarweaver.tools.workspace import MAX_EDIT_BYTES, ToolError, Workspace

        details: JSONDict = {
            "code": "RECOVERY_FILE_CONFLICT",
            "recovered_from_state": False,
            "executed": None,
        }
        relative = intent.get("path")
        if not isinstance(relative, str) or not isinstance(intent.get("after_sha256"), str):
            return "unknown", "File recovery journal lacks path or expected hash.", details
        details["path"] = relative
        try:
            workspace = Workspace(self.workspace)
            workspace.protect(self.directory)
            path = workspace.resolve(relative, must_exist=False)
            # Links and type changes cannot establish that the original file was modified.
            current = self.workspace / relative
            for candidate in (current, *current.parents):
                if candidate == self.workspace:
                    break
                if candidate.is_symlink():
                    return (
                        "unknown",
                        "File type changed to a symlink; preserved without changes.",
                        {**details, "actual_state": "symlink"},
                    )
            if not path.exists():
                if intent.get("before_sha256") is None:
                    return (
                        "blocked",
                        "New file is absent; no completed creation was observed.",
                        {
                            **details,
                            "code": "RECOVERY_NOT_EXECUTED",
                            "actual_state": "missing",
                            "executed": False,
                        },
                    )
                return (
                    "unknown",
                    "File is missing; preserved workspace without changes.",
                    {**details, "actual_state": "missing"},
                )
            if not path.is_file():
                return (
                    "unknown",
                    "Expected file has changed type; preserved without changes.",
                    {**details, "actual_state": "not_file"},
                )
            mode = stat.S_IMODE(path.stat().st_mode)
            if intent.get("mode") is not None and mode != intent["mode"]:
                return (
                    "unknown",
                    "File permissions changed; preserved without changes.",
                    {**details, "actual_state": "permissions_changed", "actual_mode": mode},
                )
            with path.open("rb") as stream:
                observed = stream.read(MAX_EDIT_BYTES + 1)
            if len(observed) > MAX_EDIT_BYTES:
                return (
                    "unknown",
                    "File grew beyond the edit limit; preserved without changes.",
                    {**details, "actual_state": "too_large"},
                )
            actual = hashlib.sha256(observed).hexdigest()
            details["actual_sha256"] = actual
            if actual == intent["after_sha256"]:
                return (
                    "ok",
                    "Expected file contents are present; recovered from file state without reapplying the edit.",
                    {
                        **intent,
                        **details,
                        "code": "RECOVERED_FROM_STATE",
                        "recovered_from_state": True,
                        "mode": mode,
                        "executed": True,
                    },
                )
            if actual == intent.get("before_sha256"):
                return (
                    "blocked",
                    "File still matches its before hash; no completed edit was observed.",
                    {**details, "code": "RECOVERY_NOT_EXECUTED", "executed": False},
                )
            return (
                "unknown",
                "File matches neither before nor after hash; preserve it and read again before editing.",
                details,
            )
        except PermissionError:
            return (
                "unknown",
                "File cannot be read because permissions changed.",
                {**details, "actual_state": "permission_denied"},
            )
        except (OSError, ValueError, ToolError) as exc:
            return (
                "unknown",
                f"File recovery cannot safely inspect the path: {exc}",
                {**details, "actual_state": "unavailable"},
            )

    def close(self) -> None:
        if not self._closed:
            try:
                self.connection.close()
            finally:
                self._lock.release()
                self._closed = True

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
