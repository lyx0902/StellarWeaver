"""PostgreSQL run journal. Each method commits a short, tenant-scoped transaction."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from stellarweaver.domain import AgentEvent, JSONDict, Message, ToolResult
from stellarweaver.runtime.loop import BudgetExceeded
from stellarweaver.session.store import SessionError, now
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web.db import QuotaReached, model_usage, scoped, unconfirmed_operations
from stellarweaver.web.delivery import Delivery
from stellarweaver.web.review import candidate


def _postgres_json(value: object) -> object:
    # JSONB rejects U+0000 even when escaped in JSON. Keep it visible in the
    # browser journal without altering delimiters used by the tool itself.
    if isinstance(value, str):
        return value.replace("\x00", "␀")
    if isinstance(value, dict):
        return {_postgres_json(key): _postgres_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_postgres_json(item) for item in value]
    return value


def pack(value: object) -> str:
    return json.dumps(_postgres_json(value), ensure_ascii=False)


class JobOwnershipLost(SessionError):
    """The worker no longer owns the persisted execution attempt."""


class PgJournal:
    last_event: AgentEvent

    def __init__(
        self,
        engine: Engine,
        tenant: str,
        session_id: str,
        run_id: str,
        workspace: Path,
        state_dir: Path,
        *,
        repair: bool = False,
        claim: tuple[str, int] | None = None,
        isolated: bool = False,
        member_id: str | None = None,
        metered: bool = False,
        chat: bool = False,
    ) -> None:
        self.engine, self.tenant = engine, tenant
        self.session_id, self.run_id = session_id, run_id
        self.workspace = workspace
        self.member_id, self.metered = member_id, metered
        self.chat = chat
        self.model_quota_exhausted = False
        self.artifacts_dir = state_dir / tenant / run_id
        self.directory = self.artifacts_dir if isolated else state_dir
        self.path = Path("postgresql") / run_id
        self.delivery = Delivery(workspace, state_dir, isolated=isolated) if repair else None
        self.isolated = isolated
        self.claim = claim
        self.connection_interrupted = False
        self.model_request_failed = False
        self._keys = {"tenant": tenant, "session": session_id, "run": run_id}

    @contextmanager
    def execution_scope(self) -> Iterator[Connection]:
        with scoped(self.engine, self.tenant) as conn:
            if self.claim is not None:
                owner, attempt = self.claim
                owned = conn.execute(
                    text("""
                    SELECT r.id FROM sw.runs r JOIN sw.run_jobs j
                    ON (r.tenant_id,r.id)=(j.tenant_id,j.run_id)
                    WHERE r.id=:run AND r.status='active' AND j.state='running'
                        AND j.owner=:owner AND j.attempt=:attempt
                    FOR UPDATE OF r
                """),
                    {**self._keys, "owner": owner, "attempt": attempt},
                ).scalar_one_or_none()
                if owned is None:
                    raise JobOwnershipLost()
            yield conn

    def cancellation_requested(self) -> bool:
        with self.execution_scope() as conn:
            cancelled = conn.execute(
                text("SELECT cancel_requested FROM sw.runs WHERE id=:run"), self._keys
            ).scalar_one()
            conn.execute(
                text("""
                UPDATE sw.run_jobs SET heartbeat_at=clock_timestamp()
                WHERE run_id=:run AND heartbeat_at < clock_timestamp() - interval '2 seconds'
            """),
                self._keys,
            )
            return bool(cancelled)

    def close(self) -> None:
        # All transactions have their own connection scope; the API owns the pool.
        pass

    def add_event_in_transaction(
        self, conn: Connection, kind: str, payload: JSONDict
    ) -> AgentEvent:
        seq = conn.execute(
            text("""
            UPDATE sw.sessions SET next_seq=next_seq+1 WHERE id=:session
            RETURNING next_seq
        """),
            self._keys,
        ).scalar_one()
        event = AgentEvent(self.session_id, self.run_id, seq, kind, payload, now())
        conn.execute(
            text("""
            INSERT INTO sw.events(tenant_id,session_id,run_id,seq,event)
            VALUES(:tenant,:session,:run,:seq,CAST(:body AS jsonb))
        """),
            {**self._keys, "seq": seq, "body": pack(asdict(event))},
        )
        self.last_event = event
        return event

    def append_event(self, kind: str, payload: JSONDict) -> AgentEvent:
        try:
            with self.execution_scope() as conn:
                if (
                    self.member_id
                    and self.metered
                    and kind
                    in {
                        "model.started",
                        "model.completed",
                        "model.failed",
                    }
                ):
                    model_usage(conn, self.tenant, self.member_id, self.run_id, kind, payload)
                window = payload.get("context_window_tokens")
                if (
                    kind == "model.started"
                    and payload.get("purpose") != "ds_search"
                    and type(window) is int
                    and window >= 2048
                ):
                    conn.execute(
                        text("""
                        UPDATE sw.runs SET context_window_tokens=:window
                        WHERE id=:run AND context_window_tokens IS NULL
                        """),
                        {**self._keys, "window": window},
                    )
                event = self.add_event_in_transaction(conn, kind, payload)
        except QuotaReached as exc:
            self.model_quota_exhausted = True
            raise BudgetExceeded("WEEKLY_MODEL_QUOTA") from exc
        if kind in {"model.started", "model.failed", "model.completed"}:
            self.model_request_failed = kind == "model.failed"
        return event

    def _message(self, conn: Connection, message: Message) -> str:
        identifier = str(uuid4())
        conn.execute(
            text("""
            INSERT INTO sw.messages(tenant_id,run_id,id,body)
            VALUES(:tenant,:run,:id,CAST(:body AS jsonb))
        """),
            {**self._keys, "id": identifier, "body": pack(message.recorded())},
        )
        return identifier

    def append_message(self, message: Message) -> str:
        if message.role == "assistant":
            return self.record_assistant(message)
        with self.execution_scope() as conn:
            return self._message(conn, message)

    def prior_turns(self) -> tuple[Message, ...]:
        """Include finished user-visible exchanges without reintroducing old tool calls."""
        with scoped(self.engine, self.tenant) as conn:
            rows = (
                conn.execute(
                    text("""
                SELECT left(r.prompt,8000) AS prompt,
                    (SELECT left(m.body->>'content',12000) FROM sw.messages m
                     WHERE m.run_id=r.id AND m.body->>'role'='assistant'
                       AND m.body->'tool_calls'='[]'::jsonb
                     ORDER BY m.position DESC LIMIT 1) AS answer
                FROM sw.runs r WHERE r.session_id=:session AND r.id<>:run
                    AND r.status='completed' AND r.review IS NULL AND r.undo IS NULL
                ORDER BY r.created_at DESC,r.id DESC LIMIT 8
                """),
                    self._keys,
                )
                .mappings()
                .all()
            )
        return tuple(
            message
            for row in reversed(rows)
            if row["answer"]
            for message in (Message("user", row["prompt"]), Message("assistant", row["answer"]))
        )

    def selected_skills(self) -> list[JSONDict]:
        with scoped(self.engine, self.tenant) as conn:
            selected: list[JSONDict] = conn.execute(
                text("SELECT skills FROM sw.runs WHERE id=:run"), self._keys
            ).scalar_one()
            return selected

    def prior_test_grants(self) -> list[JSONDict]:
        with scoped(self.engine, self.tenant) as conn:
            return list(
                conn.execute(
                    text("""
                    SELECT o.decision FROM sw.operations o JOIN sw.runs r
                        ON (r.tenant_id,r.id)=(o.tenant_id,o.run_id)
                    WHERE r.session_id=:session AND r.id<>:run
                        AND o.decision->'session_test_grant'='true'::jsonb
                    ORDER BY r.created_at DESC LIMIT 256
                """),
                    self._keys,
                ).scalars()
            )

    def latest_interruption(self) -> JSONDict | None:
        with scoped(self.engine, self.tenant) as conn:
            previous = (
                conn.execute(
                    text("""
                SELECT id,status,outcome FROM sw.runs WHERE session_id=:session AND id<>:run
                ORDER BY created_at DESC,id DESC LIMIT 1
                """),
                    self._keys,
                )
                .mappings()
                .first()
            )
            if previous is None or previous["status"] != "interrupted":
                return None
            return {
                "previous_run_id": str(previous["id"]),
                "unfinished_tools": (previous["outcome"] or {}).get("unfinished_tools", 0),
                "operations": unconfirmed_operations(conn, str(previous["id"])),
            }

    def record_assistant(self, message: Message) -> str:
        if message.role != "assistant" or len({c.call_id for c in message.tool_calls}) != len(
            message.tool_calls
        ):
            raise SessionError("Invalid assistant tool round")
        with self.execution_scope() as conn:
            identifier = self._message(conn, message)
            for call in message.tool_calls:
                conn.execute(
                    text("""
                    INSERT INTO sw.operations(tenant_id,run_id,id,assistant_id,call_id,tool_name,args)
                    VALUES(:tenant,:run,:id,:assistant,:call,:name,:args)
                """),
                    {
                        **self._keys,
                        "id": str(uuid4()),
                        "assistant": identifier,
                        "call": call.call_id,
                        "name": call.name,
                        "args": call.arguments_json,
                    },
                )
            self.add_event_in_transaction(conn, "assistant.completed", message.recorded())
            return identifier

    def operation_id_for(self, assistant_id: str, call_id: str) -> str:
        with scoped(self.engine, self.tenant) as conn:
            return str(
                conn.execute(
                    text("""
                SELECT id FROM sw.operations WHERE run_id=:run AND assistant_id=:assistant AND call_id=:call
            """),
                    {**self._keys, "assistant": assistant_id, "call": call_id},
                ).scalar_one()
            )

    def _transition(self, assistant: str, payload: JSONDict, *, intent: bool) -> AgentEvent:
        if intent and self.delivery is not None:
            self.delivery.started(payload)
        allowed = payload.get("decision") == "allow"
        with self.execution_scope() as conn:
            params = {
                **self._keys,
                "id": payload["operation_id"],
                "assistant": assistant,
                "call": payload["call_id"],
                "payload": pack(payload),
            }
            if intent:
                active = conn.execute(
                    text("""
                    SELECT status='active' AND NOT cancel_requested FROM sw.runs
                    WHERE id=:run FOR UPDATE
                """),
                    params,
                ).scalar_one()
                if not active:
                    raise asyncio.CancelledError()
                query = """
                    UPDATE sw.operations SET state='started',intent=CAST(:payload AS jsonb)
                    WHERE run_id=:run AND id=:id AND assistant_id=:assistant AND call_id=:call
                      AND state='approved' AND decision->>'decision'='allow' RETURNING id
                """
            else:
                query = """
                    UPDATE sw.operations SET state=:state,decision=CAST(:payload AS jsonb)
                    WHERE run_id=:run AND id=:id AND assistant_id=:assistant AND call_id=:call
                      AND state IN ('planned','approved') RETURNING id
                """
                params["state"] = "approved" if allowed else "planned"
            if conn.execute(text(query), params).scalar_one_or_none() is None:
                raise SessionError("Tool event did not match an eligible operation")
            return self.add_event_in_transaction(
                conn, "tool.started" if intent else "tool.authorization", payload
            )

    def record_approved(self, assistant_id: str, payload: JSONDict) -> AgentEvent:
        return self._transition(assistant_id, payload, intent=False)

    def record_intent(self, assistant_id: str, payload: JSONDict) -> AgentEvent:
        return self._transition(assistant_id, payload, intent=True)

    def record_result(self, assistant_id: str, result: ToolResult) -> AgentEvent:
        params = {**self._keys, "assistant": assistant_id, "id": result.operation_id}
        body = pack(asdict(result))
        with self.execution_scope() as conn:
            row = (
                conn.execute(
                    text("""
                SELECT * FROM sw.operations WHERE run_id=:run AND assistant_id=:assistant AND id=:id FOR UPDATE
            """),
                    params,
                )
                .mappings()
                .one()
            )
            if (row["call_id"], row["tool_name"]) != (result.call_id, result.tool_name):
                raise SessionError("Tool result does not match its planned call")
            if row["result"] is not None:
                if row["result"] != json.loads(body):
                    raise SessionError("A final tool result cannot be overwritten")
                return AgentEvent(**row["result_event"])
            if result.status == "ok" and row["state"] != "started":
                raise SessionError("Successful tool result requires persisted intent")
            event = self.add_event_in_transaction(conn, "tool.completed", asdict(result))
            conn.execute(
                text("""
                UPDATE sw.operations SET state=:status,result=CAST(:body AS jsonb),result_event=CAST(:event AS jsonb)
                WHERE run_id=:run AND id=:id
            """),
                {**params, "status": result.status, "body": body, "event": pack(asdict(event))},
            )
            self._message(conn, Message("tool", body, tool_call_id=result.call_id))
        if self.delivery is not None:
            self.delivery.result(result)
        return event

    def load_events(self) -> tuple[AgentEvent, ...]:
        with scoped(self.engine, self.tenant) as conn:
            return tuple(
                AgentEvent(**item)
                for item in conn.execute(
                    text("""
                SELECT event FROM sw.events WHERE run_id=:run ORDER BY seq
            """),
                    self._keys,
                ).scalars()
            )

    def write_checkpoint(self, summary: JSONDict, through_event_seq: int) -> str:
        identifier = str(uuid4())
        with self.execution_scope() as conn:
            conn.execute(
                text("""
                INSERT INTO sw.checkpoints VALUES (:tenant,:run,:id,:seq,CAST(:summary AS jsonb))
            """),
                {
                    **self._keys,
                    "id": identifier,
                    "seq": through_event_seq,
                    "summary": pack(summary),
                },
            )
            self.add_event_in_transaction(
                conn,
                "context.checkpoint",
                {"checkpoint_id": identifier, "through_event_seq": through_event_seq},
            )
        return identifier

    def finish_run(self, outcome: JSONDict, event_type: str) -> AgentEvent:
        if self.model_quota_exhausted:
            outcome = {**outcome, "error_code": "WEEKLY_MODEL_QUOTA", "failure_phase": "model"}
        if self.connection_interrupted:
            with self.execution_scope() as conn:
                return self.interrupt(conn, "WORKER_CONNECTION_LOST")
        if event_type == "run.failed" and "error_code" not in outcome:
            outcome = {
                **outcome,
                "error_code": "MODEL_REQUEST_FAILED"
                if self.model_request_failed
                else "RUN_EXECUTION_FAILED",
                "failure_phase": "model" if self.model_request_failed else "runtime",
            }
        report = self.delivery.report(self.run_id, outcome) if self.delivery else None
        if report is not None:
            if self.isolated:
                try:
                    report["review"] = candidate(self.workspace)
                except ToolError as exc:
                    report["review_error"] = exc.code
            outcome = {**outcome, "verification_status": report["verification"]}
        with self.execution_scope() as conn:
            return self.finish_in_transaction(conn, outcome, event_type, report)

    def interrupt(self, conn: Connection, reason: str) -> AgentEvent:
        counts = conn.execute(
            text("""
            SELECT count(*) FILTER (WHERE state='started') AS unfinished,
                   count(*) FILTER (WHERE result IS NOT NULL) AS recorded
            FROM sw.operations WHERE run_id=:run
        """),
            self._keys,
        ).one()
        waiting = conn.execute(
            text("""
            SELECT EXISTS (SELECT 1 FROM sw.approvals WHERE run_id=:run AND state='pending')
        """),
            self._keys,
        ).scalar_one()
        unfinished = max(counts.unfinished, len(unconfirmed_operations(conn, self.run_id)))
        return self.finish_in_transaction(
            conn,
            {
                "run_status": "interrupted",
                "verification_status": "unknown" if unfinished else "not_run",
                "error_code": reason,
                "failure_phase": "tool" if unfinished else "approval" if waiting else "worker",
                "unfinished_tools": unfinished,
                "recorded_results": counts.recorded,
            },
            "run.interrupted",
        )

    def finish_in_transaction(
        self, conn: Connection, outcome: JSONDict, event_type: str, report: JSONDict | None = None
    ) -> AgentEvent:
        cancelled = conn.execute(
            text(
                "SELECT cancel_requested FROM sw.runs WHERE id=:run AND status='active' FOR UPDATE"
            ),
            self._keys,
        ).scalar_one_or_none()
        if cancelled is None:
            raise SessionError("Run is no longer active")
        if cancelled and event_type != "run.interrupted":
            event_type = "run.cancelled"
            outcome = {**outcome, "run_status": "cancelled"}
            if report is not None:
                report["run_status"] = "cancelled"
                if report["verification"] == "passed":
                    report["verification"] = "stale"
                outcome["verification_status"] = report["verification"]
        if (
            conn.execute(
                text("""
            UPDATE sw.runs SET status=:status,outcome=CAST(:outcome AS jsonb),report=CAST(:report AS jsonb)
            WHERE id=:run AND status='active' RETURNING id
        """),
                {
                    **self._keys,
                    "status": outcome["run_status"],
                    "outcome": pack(outcome),
                    "report": pack(report) if report is not None else None,
                },
            ).scalar_one_or_none()
            is None
        ):
            raise SessionError("Run is no longer active")
        conn.execute(
            text("""
            UPDATE sw.approvals SET state='cancelled' WHERE run_id=:run AND state IN ('pending','approved')
        """),
            self._keys,
        )
        conn.execute(
            text("UPDATE sw.run_jobs SET state='done' WHERE run_id=:run AND state<>'done'"),
            self._keys,
        )
        return self.add_event_in_transaction(conn, event_type, outcome)
