"""Revert journaled built-in file changes inside the saved task worktree."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from stellarweaver.changes import UndoFile
from stellarweaver.domain import JSONDict
from stellarweaver.session.locking import workspace_write_lock
from stellarweaver.tools.workspace import ToolError, Workspace
from stellarweaver.web.db import audit
from stellarweaver.web.journal import PgJournal


def latest_edit(conn: Connection, session: str) -> JSONDict | None:
    row = (
        conn.execute(
            text("""
        SELECT o.id::text AS operation_id, r.id::text AS source_run_id,
            o.tool_name, o.result->'data' AS data
        FROM sw.operations o JOIN sw.runs r ON (r.tenant_id,r.id)=(o.tenant_id,o.run_id)
        WHERE r.session_id=:session AND r.review IS NULL AND o.state='ok'
            AND o.tool_name IN ('create_file','edit_file')
            AND NOT EXISTS (
                SELECT 1 FROM sw.operations pending JOIN sw.runs prior
                    ON (prior.tenant_id,prior.id)=(pending.tenant_id,pending.run_id)
                WHERE prior.session_id=:session AND pending.intent IS NOT NULL
                    AND pending.tool_name IN ('create_file','edit_file','apply_patch')
                    AND pending.state IN ('started','unknown')
            )
            AND NOT EXISTS (
                SELECT 1 FROM sw.events e WHERE e.session_id=:session
                    AND e.event->'payload'->>'target_operation_id'=o.id::text
                    AND e.event->>'type' IN ('changes.undo.started','changes.undo.completed')
            )
            AND NOT EXISTS (
                SELECT 1 FROM sw.events started WHERE started.session_id=:session
                   AND started.event->>'type'='changes.undo.started'
                   AND NOT EXISTS (
                     SELECT 1 FROM sw.events finish WHERE finish.run_id=started.run_id
                       AND finish.event->>'type'='changes.undo.completed'
                   )
            )
        ORDER BY (o.result_event->>'seq')::bigint DESC LIMIT 1
        """),
            {"session": session},
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def undo_edit(journal: PgJournal, request: JSONDict) -> None:
    if not journal.isolated or journal.delivery is None or not journal.claim:
        raise ToolError("UNDO_ISOLATION_REQUIRED", "Undo requires a saved private worktree.")
    workspace = Workspace(journal.workspace)
    workspace.protect(journal.directory)
    with workspace_write_lock(workspace.root):
        with journal.execution_scope() as conn:
            edit = latest_edit(conn, journal.session_id)
            if edit is None or edit["operation_id"] != request["operation_id"]:
                raise ToolError("UNDO_TARGET_CHANGED", "The latest undoable operation has changed.")
            if conn.execute(
                text("SELECT cancel_requested FROM sw.runs WHERE id=:run"),
                {"run": journal.run_id},
            ).scalar_one():
                journal.finish_in_transaction(conn, {"run_status": "cancelled"}, "run.cancelled")
                return
            data = edit["data"]
            if (edit["tool_name"] == "edit_file") != isinstance(data.get("before_sha256"), str):
                raise ToolError("UNDO_METADATA_MISSING", "Invalid before hash for this operation.")
            artifacts = journal.artifacts_dir.parent / str(UUID(edit["source_run_id"]))
            file = UndoFile(workspace, artifacts)
            path, previous, mode = file.checked_target(data)
            details = {
                "path": data["path"],
                "target_operation_id": edit["operation_id"],
                "source_run_id": edit["source_run_id"],
                "expected_sha256": data["after_sha256"],
                "restore_sha256": data.get("before_sha256"),
            }
            journal.add_event_in_transaction(conn, "changes.undo.started", details)
            audit(
                conn,
                journal.tenant,
                journal.member_id,
                journal.run_id,
                "undo.started",
                edit["operation_id"],
            )
        try:
            file.restore(path, previous, mode)
        except OSError:
            journal.finish_run(
                {
                    "run_status": "interrupted",
                    "unfinished_tools": 1,
                    "error_code": "UNDO_STATE_UNKNOWN",
                    "failure_phase": "undo",
                },
                "run.interrupted",
            )
            return
        journal.append_event("changes.undo.completed", {**details, "status": "ok"})
    journal.finish_run(
        {"run_status": "completed", "final_text": f"已撤销隔离工作区中的文件修改：{data['path']}"},
        "run.completed",
    )
