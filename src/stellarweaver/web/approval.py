"""Run-bound one-shot approval, backed by the same durable event log."""

import asyncio
import sys
from threading import Event
from typing import Literal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from stellarweaver.domain import JSONDict
from stellarweaver.policy import DS_SEARCH
from stellarweaver.policy.risk import fingerprint
from stellarweaver.tools.development import decode_development
from stellarweaver.web.db import audit, scoped
from stellarweaver.web.journal import PgJournal, pack

# Exact host command for explicitly trusted local repositories only.
VERIFY_ARGV = [sys.executable, "-B", "-m", "unittest", "-q"]


class DecisionConflict(Exception):
    """Operation changed, expired, or no longer awaits a decision."""


def lock_run(conn: Connection, run_id: str) -> JSONDict:
    row = (
        conn.execute(
            text("SELECT status,cancel_requested FROM sw.runs WHERE id=:run FOR UPDATE"),
            {"run": run_id},
        )
        .mappings()
        .one_or_none()
    )
    return {} if row is None else dict(row)


def pending(conn: Connection, run_id: str) -> JSONDict | None:
    row = (
        conn.execute(
            text("""
        SELECT id, preview, expires_at FROM sw.approvals
        WHERE run_id=:run AND state='pending' AND expires_at>clock_timestamp()
    """),
            {"run": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return {**row["preview"], "id": str(row["id"]), "expires_at": row["expires_at"].isoformat()}


def decide(journal: PgJournal, identifier: str, decision: Literal["approve", "deny"]) -> None:
    with scoped(journal.engine, journal.tenant) as conn:
        status = lock_run(conn, journal.run_id)
        if status.get("status") != "active" or status.get("cancel_requested"):
            raise DecisionConflict()
        state = "approved" if decision == "approve" else "denied"
        accepted = conn.execute(
            text("""
            UPDATE sw.approvals SET state=:state WHERE tenant_id=:tenant AND run_id=:run AND id=:id
              AND state='pending' AND expires_at>clock_timestamp() RETURNING id
        """),
            {"state": state, "tenant": journal.tenant, "run": journal.run_id, "id": identifier},
        ).scalar_one_or_none()
        if accepted is None:
            raise DecisionConflict()
        journal.add_event_in_transaction(
            conn, "approval.decided", {"id": identifier, "decision": state}
        )
        audit(
            conn,
            journal.tenant,
            journal.member_id,
            journal.run_id,
            f"approval.{decision}",
            identifier,
        )


def request_cancel(journal: PgJournal) -> None:
    with scoped(journal.engine, journal.tenant) as conn:
        status = lock_run(conn, journal.run_id)
        if status.get("status") != "active":
            raise DecisionConflict()
        if status["cancel_requested"]:
            return
        conn.execute(
            text("UPDATE sw.runs SET cancel_requested=true WHERE id=:run"), {"run": journal.run_id}
        )
        conn.execute(
            text("""
            UPDATE sw.approvals SET state='cancelled' WHERE run_id=:run AND state IN ('pending','approved')
        """),
            {"run": journal.run_id},
        )
        journal.add_event_in_transaction(conn, "run.cancel_requested", {})
        audit(conn, journal.tenant, journal.member_id, journal.run_id, "run.cancel")
        queued = conn.execute(
            text("SELECT state='queued' FROM sw.run_jobs WHERE run_id=:run"),
            {"run": journal.run_id},
        ).scalar_one_or_none()
        if queued:
            journal.finish_in_transaction(
                conn, {"run_status": "cancelled", "verification_status": "not_run"}, "run.cancelled"
            )


class ApprovalGate:
    def __init__(
        self,
        journal: PgJournal,
        stop: Event,
        ttl: float,
        commands: tuple[tuple[str, ...], ...] = (tuple(VERIFY_ARGV),),
        *,
        development: bool = False,
    ) -> None:
        self.journal, self.stop, self.ttl = journal, stop, ttl
        self.commands = commands
        self.development = development

    async def approve(self, invocation: JSONDict) -> str:
        tool = invocation["tool"]
        if tool == "run_command":
            argv = invocation["argv"]
            check = decode_development(argv) if self.development else None
            if (
                invocation.get("cwd") != "."
                or (check is None and tuple(argv) not in self.commands)
                or (check is not None and invocation.get("development_action") != check[0])
            ):
                return "deny"
            preview = {
                "tool": check[0] if check else tool,
                "argv": [check[1].framework, check[1].target] if check else argv,
                "cwd": ".",
                "timeout_seconds": invocation["timeout_seconds"],
            }
        elif tool in {"edit_file", "create_file", "stage_patch"}:
            patch = invocation.get("preview")
            if not isinstance(patch, str) or len(patch.encode()) > 64 * 1024:
                # A partial patch is not a sufficient consent preview.
                return "deny"
            preview = {
                "tool": tool,
                "diff": patch,
            }
            if tool == "stage_patch":
                preview.update(
                    {key: invocation[key] for key in ("base_commit", "patch_sha256", "paths")}
                )
            else:
                preview.update(
                    path=invocation["path"],
                    before_sha256=invocation.get("before_sha256"),
                    after_sha256=invocation.get("after_sha256"),
                )
        elif tool == DS_SEARCH:
            preview = {"tool": tool, "query": invocation["query"]}
        else:
            return "deny"
        if "security" in invocation:
            preview["security"] = invocation["security"]
        operation = invocation["operation_id"]
        params = {
            "tenant": self.journal.tenant,
            "run": self.journal.run_id,
            "id": operation,
            "preview": pack(preview),
            "fingerprint": fingerprint(
                {
                    "tenant": self.journal.tenant,
                    "run": self.journal.run_id,
                    "invocation": invocation,
                }
            ),
            "ttl": self.ttl,
        }
        with self.journal.execution_scope() as conn:
            status = lock_run(conn, self.journal.run_id)
            if (
                self.stop.is_set()
                or status.get("status") != "active"
                or status.get("cancel_requested")
            ):
                raise asyncio.CancelledError()
            expiry = conn.execute(
                text("""
                INSERT INTO sw.approvals(tenant_id,run_id,id,state,expires_at,preview,fingerprint)
                VALUES(:tenant,:run,:id,'pending',clock_timestamp()+CAST(:ttl AS double precision)*interval '1 second',
                       CAST(:preview AS jsonb),:fingerprint)
                ON CONFLICT (tenant_id,run_id,id) DO NOTHING RETURNING expires_at
            """),
                params,
            ).scalar_one_or_none()
            if expiry is None:
                return "deny"
            self.journal.add_event_in_transaction(
                conn,
                "approval.requested",
                {
                    **preview,
                    "id": operation,
                    "expires_at": expiry.isoformat(),
                },
            )
        while not self.stop.is_set():
            with self.journal.execution_scope() as conn:
                status = lock_run(conn, self.journal.run_id)
                if status.get("status") != "active" or status.get("cancel_requested"):
                    raise asyncio.CancelledError()
                expired = conn.execute(
                    text("""
                    UPDATE sw.approvals SET state='expired'
                    WHERE tenant_id=:tenant AND run_id=:run AND id=:id AND state IN ('pending','approved')
                        AND expires_at<=clock_timestamp() RETURNING id
                """),
                    params,
                ).scalar_one_or_none()
                if expired is not None:
                    self.journal.add_event_in_transaction(
                        conn, "approval.expired", {"id": operation}
                    )
                    return "deny"
                used = conn.execute(
                    text("""
                    UPDATE sw.approvals SET state='consumed'
                    WHERE tenant_id=:tenant AND run_id=:run AND id=:id AND state='approved'
                        AND fingerprint=:fingerprint AND expires_at>clock_timestamp() RETURNING id
                """),
                    params,
                ).scalar_one_or_none()
                if used is not None:
                    self.journal.add_event_in_transaction(
                        conn, "approval.consumed", {"id": operation}
                    )
                    return "once"
                state = conn.execute(
                    text("""
                    SELECT state FROM sw.approvals WHERE tenant_id=:tenant AND run_id=:run AND id=:id
                """),
                    params,
                ).scalar_one()
                if state == "approved":
                    conn.execute(
                        text("""
                        UPDATE sw.approvals SET state='cancelled'
                        WHERE tenant_id=:tenant AND run_id=:run AND id=:id AND state='approved'
                        """),
                        params,
                    )
                    self.journal.add_event_in_transaction(
                        conn, "approval.invalidated", {"id": operation}
                    )
                if state != "pending":
                    return "deny"
            await asyncio.sleep(0.15)
        raise asyncio.CancelledError()
