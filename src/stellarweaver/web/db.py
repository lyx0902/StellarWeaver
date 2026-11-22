"""PostgreSQL transactions for the local web entrypoint."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4, uuid5

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError

from stellarweaver.domain import JSONDict
from stellarweaver.web.identity import Access, Member

SERVER_LOCK = 1645207841
WORKER_LOCK = 1645207842
RunMode = Literal["read-only", "repair", "auto"]
REPAIR_MODES = frozenset({"repair", "auto"})
_ACCESS = """
    (CAST(:workspace_ids AS uuid[]) IS NULL OR s.workspace_id=ANY(CAST(:workspace_ids AS uuid[])))
    AND (CAST(:owner AS uuid) IS NULL OR s.member_id=CAST(:owner AS uuid))
    AND (CAST(:actor AS uuid) IS NULL OR EXISTS (
        SELECT 1 FROM sw.workspace_grants g WHERE (g.tenant_id,g.workspace_id)=(s.tenant_id,s.workspace_id)
          AND g.member_id=CAST(:actor AS uuid)
    ))
"""


def _visible(access: Access | None) -> dict[str, str | list[str] | None]:
    return (
        access.bindings()
        if access is not None
        else {"workspace_ids": None, "owner": None, "actor": None}
    )


def engine_for(dsn: str) -> Engine:
    if not dsn.startswith("postgresql+psycopg://"):
        raise ValueError("A postgresql+psycopg DSN is required")
    return create_engine(
        dsn,
        pool_pre_ping=True,
        pool_timeout=5,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )


def migrate(dsn: str) -> None:
    settings = Config()
    settings.set_main_option("script_location", str(Path(__file__).with_name("migrations")))
    engine = engine_for(dsn)
    try:
        with engine.connect() as connection:
            settings.attributes["connection"] = connection
            command.upgrade(settings, "head")
    finally:
        engine.dispose()


@contextmanager
def tenant_scope(conn: Connection, tenant_id: str) -> Iterator[Connection]:
    with conn.begin():
        conn.execute(
            text("SELECT set_config('sw.tenant_id', :tenant, true)"), {"tenant": tenant_id}
        )
        yield conn


@contextmanager
def scoped(engine: Engine, tenant_id: str) -> Iterator[Connection]:
    with engine.connect() as conn, tenant_scope(conn, tenant_id):
        yield conn


@contextmanager
def service_lock(engine: Engine, *, queued: bool, worker: bool = False) -> Iterator[Connection]:
    locks = [(SERVER_LOCK, queued)]
    if worker:
        locks.append((WORKER_LOCK, False))
    with engine.connect() as conn:
        if not conn.execute(
            text("""
            SELECT NOT(rolsuper OR rolbypassrls) AND rolname='stellarweaver_app'
            FROM pg_roles WHERE rolname=current_user
        """)
        ).scalar_one():
            raise RuntimeError("Use the non-privileged stellarweaver_app role")
        if not conn.execute(text("SELECT to_regclass('sw.run_jobs') IS NOT NULL")).scalar_one():
            raise RuntimeError("Apply the web schema with python -m stellarweaver.web migrate")
        acquired: list[tuple[int, bool]] = []
        try:
            for key, shared in locks:
                suffix = "_shared" if shared else ""
                if not conn.execute(
                    text(f"SELECT pg_try_advisory_lock{suffix}(:key)"), {"key": key}
                ).scalar_one():
                    raise RuntimeError("Another process owns the workbench execution mode")
                acquired.append((key, shared))
            conn.commit()
            yield conn
        finally:
            if not conn.invalidated:
                try:
                    for key, shared in reversed(acquired):
                        suffix = "_shared" if shared else ""
                        conn.execute(text(f"SELECT pg_advisory_unlock{suffix}(:key)"), {"key": key})
                    conn.commit()
                except DBAPIError as exc:
                    if not exc.connection_invalidated:
                        raise


def register(engine: Engine, tenant: str, workspaces: dict[str, str]) -> None:
    with scoped(engine, tenant) as conn:
        conn.execute(
            text("INSERT INTO sw.tenants VALUES (:tenant) ON CONFLICT DO NOTHING"),
            {"tenant": tenant},
        )
        for identifier, label in workspaces.items():
            conn.execute(
                text("""
                INSERT INTO sw.workspaces(tenant_id,id,label) VALUES (:tenant,:id,:label)
                ON CONFLICT (tenant_id,id) DO UPDATE SET label=excluded.label
            """),
                {"tenant": tenant, "id": identifier, "label": label},
            )


def register_member(engine: Engine, member: Member, workspace_ids: list[str]) -> None:
    with scoped(engine, str(member.tenant)) as conn:
        params = {
            "tenant": member.tenant,
            "id": member.id,
            "name": member.name,
            "role": member.role,
            "active": member.active_limit,
            "limit": member.weekly_tokens,
        }
        conn.execute(
            text("""
            INSERT INTO sw.memberships(tenant_id,id,name,role,active_limit,weekly_tokens)
            VALUES(:tenant,:id,:name,:role,:active,:limit)
            ON CONFLICT (tenant_id,id) DO UPDATE SET name=excluded.name,role=excluded.role,
              active_limit=excluded.active_limit,weekly_tokens=excluded.weekly_tokens
        """),
            params,
        )
        conn.execute(
            text("""DELETE FROM sw.workspace_grants WHERE member_id=:id
                    AND NOT (workspace_id=ANY(CAST(:workspaces AS uuid[])))"""),
            {**params, "workspaces": workspace_ids},
        )
        for workspace in workspace_ids:
            conn.execute(
                text("""
                INSERT INTO sw.workspace_grants(tenant_id,member_id,workspace_id)
                VALUES(:tenant,:id,:workspace)
                ON CONFLICT DO NOTHING
            """),
                {**params, "workspace": workspace},
            )


def adopt_legacy_threads(engine: Engine, tenant: str, access: Access) -> None:
    with scoped(engine, tenant) as conn:
        assign_legacy_threads(conn, access)


def assign_legacy_threads(conn: Connection, access: Access) -> None:
    conn.execute(
        text("""
            WITH adopted AS (
                UPDATE sw.sessions s SET member_id=:actor
                WHERE s.member_id IS NULL AND s.workspace_id=ANY(CAST(:workspace_ids AS uuid[]))
                  AND EXISTS (SELECT 1 FROM sw.workspace_grants g WHERE
                      (g.tenant_id,g.workspace_id,g.member_id)=(s.tenant_id,s.workspace_id,:actor))
                RETURNING tenant_id,id
            )
            UPDATE sw.runs r SET member_id=:actor FROM adopted s
            WHERE (r.tenant_id,r.session_id)=(s.tenant_id,s.id) AND r.member_id IS NULL
        """),
        access.bindings(),
    )


def audit(
    conn: Connection,
    tenant: str,
    member: str | None,
    run: str,
    action: str,
    operation: str | None = None,
) -> None:
    if member is not None:
        conn.execute(
            text("""
            INSERT INTO sw.audit(tenant_id,member_id,run_id,operation_id,action)
            VALUES(:tenant,:member,:run,:operation,:action)
        """),
            {
                "tenant": tenant,
                "member": member,
                "run": run,
                "action": action,
                "operation": operation,
            },
        )


def audit_rows(engine: Engine, tenant: str, access: Access) -> list[JSONDict]:
    with scoped(engine, tenant) as conn:
        rows = conn.execute(
            text(f"""
            SELECT a.id,a.member_id::text,m.name,a.run_id::text,a.operation_id::text,a.action,a.created_at
            FROM sw.audit a JOIN sw.memberships m ON (m.tenant_id,m.id)=(a.tenant_id,a.member_id)
            JOIN sw.runs r ON (r.tenant_id,r.id)=(a.tenant_id,a.run_id)
            JOIN sw.sessions s ON (s.tenant_id,s.id)=(r.tenant_id,r.session_id)
            WHERE {_ACCESS} ORDER BY a.id DESC LIMIT 100
        """),
            _visible(access),
        )
        return [dict(row) for row in rows.mappings()]


class QuotaReached(Exception):
    """Configured queue or token allowance exhausted."""


def project_skill_trust(
    engine: Engine, tenant: str, member: str | None, workspace: str, enabled: bool | None = None
) -> bool | None:
    if member is None:
        return None
    with scoped(engine, tenant) as conn:
        params = {"member": member, "workspace": workspace, "enabled": enabled}
        if enabled is None:
            query = """SELECT project_skills FROM sw.workspace_grants
                       WHERE member_id=:member AND workspace_id=:workspace"""
        else:
            query = """UPDATE sw.workspace_grants SET project_skills=:enabled
                       WHERE member_id=:member AND workspace_id=:workspace RETURNING project_skills"""
        value: bool | None = conn.execute(text(query), params).scalar_one_or_none()
        return value


def _usage_limit(conn: Connection, member: str) -> tuple[int, int, int, datetime]:
    limit = conn.execute(
        text("""
        SELECT active_limit,weekly_tokens FROM sw.memberships WHERE id=:member FOR UPDATE
    """),
        {"member": member},
    ).one()
    measured = conn.execute(
        text("""
        WITH bounds AS (
            SELECT statement_timestamp() AS at,
                   date_trunc('week',statement_timestamp() AT TIME ZONE 'UTC')::date AS start
        )
        SELECT bounds.at, (
            SELECT COALESCE(sum(tokens),0) FROM sw.model_usage WHERE member_id=:member
              AND usage_day>=bounds.start AND usage_day<(bounds.start+7)
        ) AS used FROM bounds
    """),
        {"member": member},
    ).one()
    return limit.active_limit, limit.weekly_tokens, int(measured.used), measured.at


def model_usage(
    conn: Connection, tenant: str, member: str, run: str, kind: str, payload: JSONDict
) -> None:
    params = {"tenant": tenant, "member": member, "run": run, "index": payload["request_index"]}
    if kind == "model.started":
        _, limit, used, started_at = _usage_limit(conn, member)
        reserve = payload["input_tokens"] + payload["max_output_tokens"]
        if used + reserve > limit:
            raise QuotaReached()
        conn.execute(
            text("""
            INSERT INTO sw.model_usage(tenant_id,run_id,request_index,member_id,tokens,usage_day,started_at)
            VALUES(:tenant,:run,:index,:member,:tokens,
                   (CAST(:at AS timestamptz) AT TIME ZONE 'UTC')::date, :at)
        """),
            {**params, "tokens": reserve, "at": started_at},
        )
    elif kind == "model.completed" or (kind == "model.failed" and not payload["estimated"]):
        # Serialize refunds with reservations for the same member.
        _usage_limit(conn, member)
        conn.execute(
            text("""
            UPDATE sw.model_usage SET tokens=:tokens,settled=true,estimated=:estimated
            WHERE run_id=:run AND request_index=:index AND member_id=:member
        """),
            {
                **params,
                "tokens": payload["input_tokens"] + payload["output_tokens"],
                "estimated": payload["estimated"],
            },
        )


def usage_rows(engine: Engine, tenant: str, access: Access) -> list[JSONDict]:
    with scoped(engine, tenant) as conn:
        return _usage_rows(conn, access)


def _usage_rows(conn: Connection, access: Access) -> list[JSONDict]:
    rows = conn.execute(
        text(f"""
            SELECT u.member_id::text,m.name,u.usage_day,m.weekly_tokens,
                sum(u.tokens)::bigint AS tokens,count(*) AS requests,
                count(*) FILTER (WHERE u.estimated) AS estimated_requests
            FROM sw.model_usage u JOIN sw.memberships m ON (m.tenant_id,m.id)=(u.tenant_id,u.member_id)
            JOIN sw.runs r ON (r.tenant_id,r.id)=(u.tenant_id,u.run_id)
            JOIN sw.sessions s ON (s.tenant_id,s.id)=(r.tenant_id,r.session_id)
            WHERE {_ACCESS}
            GROUP BY u.member_id,m.name,u.usage_day,m.weekly_tokens
            ORDER BY u.usage_day DESC,u.member_id LIMIT 100
        """),
        _visible(access),
    )
    return [dict(row) for row in rows.mappings()]


def usage_today(engine: Engine, tenant: str, access: Access, *, at: str | None = None) -> JSONDict:
    with scoped(engine, tenant) as conn:
        clock = (
            conn.execute(
                text("""
            WITH clock AS (
                SELECT COALESCE(CAST(:at AS timestamptz),clock_timestamp()) AS read_at
            )
            SELECT read_at,(read_at AT TIME ZONE 'Asia/Shanghai')::date AS day,
                   date_trunc('week',read_at AT TIME ZONE 'UTC')::date AS quota_week_start,
                   ((date_trunc('week',read_at AT TIME ZONE 'UTC')+interval '7 days') AT TIME ZONE 'UTC')
                       AS quota_resets_at
            FROM clock
            """),
                {"at": at},
            )
            .mappings()
            .one()
        )
        today = (
            conn.execute(
                text(f"""
            SELECT coalesce(sum(u.tokens),0)::bigint AS tokens,count(*) AS requests,
                   count(*) FILTER (WHERE u.estimated) AS estimated_requests,
                   count(*) FILTER (WHERE NOT u.settled) AS unsettled_requests
            FROM sw.model_usage u
            JOIN sw.runs r ON (r.tenant_id,r.id)=(u.tenant_id,u.run_id)
            JOIN sw.sessions s ON (s.tenant_id,s.id)=(r.tenant_id,r.session_id)
            WHERE {_ACCESS}
              AND u.started_at >= (CAST(:day AS timestamp) AT TIME ZONE 'Asia/Shanghai')
              AND u.started_at < ((CAST(:day AS date)+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
            """),
                {"day": clock["day"], **_visible(access)},
            )
            .mappings()
            .one()
        )
        quota = (
            conn.execute(
                text("""
            SELECT m.weekly_tokens AS limit_tokens,coalesce(sum(u.tokens),0)::bigint AS used_tokens
            FROM sw.memberships m LEFT JOIN sw.model_usage u
              ON (u.tenant_id,u.member_id)=(m.tenant_id,m.id)
              AND u.usage_day>=:week_start AND u.usage_day<(CAST(:week_start AS date)+7)
            WHERE m.id=:member GROUP BY m.weekly_tokens
            """),
                {"member": access.actor, "week_start": clock["quota_week_start"]},
            )
            .mappings()
            .one()
        )
        return {
            **clock,
            "today": dict(today),
            "quota": dict(quota),
            "history": _usage_rows(conn, access),
        }


class RequestConflict(Exception):
    """An idempotency key was already used with different input."""


class RunUnavailable(Exception):
    """The server cannot start another run."""


class ExecutionModeConflict(Exception):
    """The database is currently owned by an inline service."""


class ThreadUnavailable(Exception):
    """The thread cannot accept this follow-up."""


class InterruptedConfirmationRequired(Exception):
    """The previous run needs an explicit recovery choice."""


def submit(
    engine: Engine,
    tenant: str,
    workspace: str,
    key: str,
    prompt: str,
    provider: str,
    mode: str = "read-only",
    *,
    allow_create: bool = True,
    enqueue: bool = False,
    session_id: str | None = None,
    continuation: str | None = None,
    expected_previous_run: str | None = None,
    acknowledge_interrupted: bool = False,
    isolated: bool = False,
    review: JSONDict | None = None,
    member: str | None = None,
    skills: list[JSONDict] | None = None,
    undo: JSONDict | None = None,
    context_window_tokens: int | None = None,
) -> tuple[JSONDict, bool]:
    if member is not None:
        key = str(uuid5(UUID(member), key))
    params = {
        "tenant": tenant,
        "workspace": workspace,
        "key": key,
        "prompt": prompt,
        "provider": provider,
        "mode": mode,
        "session": session_id or str(uuid4()),
        "id": str(uuid4()),
        "continuation": continuation,
        "isolated": isolated,
        "review": json.dumps(review) if review is not None else None,
        "skills": json.dumps(skills or []),
        "undo": json.dumps(undo) if undo is not None else None,
        "context_window_tokens": context_window_tokens,
        "member": member,
    }
    with scoped(engine, tenant) as conn:
        if (
            enqueue
            and not conn.execute(
                text("SELECT pg_try_advisory_xact_lock_shared(:key)"), {"key": SERVER_LOCK}
            ).scalar_one()
        ):
            raise ExecutionModeConflict()
        # Serialize deduplication in this transaction, even with concurrent API requests.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": tenant + key})
        existing = (
            conn.execute(
                text("""
            SELECT r.id,r.session_id,r.prompt,r.provider,r.mode,r.continuation,r.review,r.skills,r.undo,
                s.workspace_id,s.isolated,r.member_id::text AS member_id
            FROM sw.runs r JOIN sw.sessions s ON (r.tenant_id,r.session_id)=(s.tenant_id,s.id)
            WHERE r.request_key=:key
        """),
                params,
            )
            .mappings()
            .first()
        )
        if existing:
            if (
                existing["member_id"] != member
                or (
                    str(existing["workspace_id"]),
                    existing["prompt"],
                    existing["provider"],
                    existing["mode"],
                    existing["continuation"],
                    existing["review"],
                    existing["skills"] if skills is not None else [],
                    existing["undo"],
                    existing["isolated"],
                )
                != (
                    workspace,
                    prompt,
                    provider,
                    mode,
                    continuation,
                    review,
                    skills or [],
                    undo,
                    isolated,
                )
                or (session_id is not None and str(existing["session_id"]) != session_id)
            ):
                raise RequestConflict()
            return {"run_id": str(existing["id"]), "session_id": str(existing["session_id"])}, False
        if enqueue:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:scope), 3)"), {"scope": tenant}
            )
            queued_count = conn.execute(
                text("SELECT count(*) FROM sw.run_jobs WHERE state<>'done'")
            ).scalar_one()
            if queued_count >= 50:
                raise RunUnavailable()
        if member is not None:
            active_limit, weekly_tokens, used, _ = _usage_limit(conn, member)
            counts = (
                conn.execute(
                    text("""
                SELECT count(*) FILTER (WHERE status='active') AS active,
                    count(*) FILTER (WHERE created_at > now() - interval '1 minute') AS recent
                FROM sw.runs WHERE member_id=:member
                    AND (status='active' OR created_at > now() - interval '1 minute')
            """),
                    params,
                )
                .mappings()
                .one()
            )
            if (
                (
                    provider == "deepseek"
                    and undo is None
                    and review is None
                    and used >= weekly_tokens
                )
                or counts["active"] >= active_limit
                or counts["recent"] >= 20
            ):
                raise QuotaReached()
            if (
                conn.execute(
                    text("""
                SELECT 1 FROM sw.workspace_grants
                WHERE member_id=:member AND workspace_id=:workspace
            """),
                    params,
                ).scalar_one_or_none()
                is None
            ):
                raise ThreadUnavailable()
        if not allow_create:
            raise RunUnavailable()
        if session_id is None:
            conn.execute(
                text("""INSERT INTO sw.sessions(tenant_id,id,workspace_id,isolated,member_id)
                     VALUES (:tenant,:session,:workspace,:isolated,:member)"""),
                params,
            )
        else:
            thread = (
                conn.execute(
                    text("""SELECT workspace_id,isolated,archived_at,member_id::text AS member_id FROM sw.sessions
                     WHERE id=:session FOR UPDATE"""),
                    params,
                )
                .mappings()
                .first()
            )
            if (
                thread is None
                or str(thread["workspace_id"]) != workspace
                or thread["isolated"] != isolated
                or thread["archived_at"] is not None
                or (member is not None and thread["member_id"] != member)
            ):
                raise ThreadUnavailable()
            previous = (
                conn.execute(
                    text("""SELECT id,status,outcome,skills,review IS NOT NULL AS is_review
                     FROM sw.runs WHERE session_id=:session ORDER BY created_at DESC,id DESC LIMIT 1"""),
                    params,
                )
                .mappings()
                .one()
            )
            if (
                previous["status"] == "active"
                or previous["is_review"]
                or (
                    expected_previous_run is not None
                    and str(previous["id"]) != expected_previous_run
                )
            ):
                raise ThreadUnavailable()
            if (
                previous["status"] == "interrupted"
                or (previous["outcome"] or {}).get("unfinished_tools")
            ) and not acknowledge_interrupted:
                raise InterruptedConfirmationRequired()
            if skills is None:
                params["skills"] = json.dumps(previous["skills"])
        conn.execute(
            text("""
            INSERT INTO sw.runs(tenant_id,id,session_id,request_key,prompt,provider,mode,continuation,
                                review,member_id,skills,undo,context_window_tokens)
            VALUES (:tenant,:id,:session,:key,:prompt,:provider,:mode,:continuation,
                    CAST(:review AS jsonb),:member,CAST(:skills AS jsonb),CAST(:undo AS jsonb),
                    :context_window_tokens)
        """),
            params,
        )
        if enqueue:
            conn.execute(
                text("INSERT INTO sw.run_jobs(tenant_id,run_id) VALUES (:tenant,:id)"), params
            )
        action = "undo.submit" if undo else "review.submit" if review else "run.submit"
        audit(conn, tenant, member, str(params["id"]), action)
        return {"run_id": params["id"], "session_id": params["session"]}, True


def snapshot(
    engine: Engine, tenant: str, run_id: str, access: Access | None = None
) -> JSONDict | None:
    with scoped(engine, tenant) as conn:
        row = (
            conn.execute(
                text(f"""
            SELECT r.id AS run_id,r.session_id,r.status,r.prompt,r.provider,r.mode,r.created_at,
                r.cancel_requested,r.outcome,r.report IS NOT NULL AS has_report,
                r.outcome->>'final_text' AS final_text,r.context_window_tokens,
                s.workspace_id,s.isolated,s.next_seq AS through_seq,j.state AS job_state,
                r.continuation,r.review IS NOT NULL AS is_review,r.skills,(r.undo IS NOT NULL) AS is_undo,
                s.member_id::text AS member_id
            FROM sw.runs r JOIN sw.sessions s ON (r.tenant_id,r.session_id)=(s.tenant_id,s.id)
            LEFT JOIN sw.run_jobs j ON (r.tenant_id,r.id)=(j.tenant_id,j.run_id)
            WHERE r.id=:id AND {_ACCESS}
        """),
                {"id": run_id, **_visible(access)},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        result = {**row, **{k: str(row[k]) for k in ("run_id", "session_id", "workspace_id")}}
        if row["status"] == "interrupted":
            result["unconfirmed_operations"] = unconfirmed_operations(conn, run_id)
        return result


def unconfirmed_operations(conn: Connection, run_id: str) -> list[JSONDict]:
    return [
        dict(row)
        for row in conn.execute(
            text("""
                SELECT id::text AS operation_id,tool_name,intent->>'path' AS path,
                    intent->'argv' AS argv
                FROM sw.operations WHERE run_id=:id
                  AND (state='started' OR (state='unknown' AND intent IS NOT NULL))
                UNION ALL
                SELECT event->'payload'->>'target_operation_id','undo',
                    event->'payload'->>'path',NULL::jsonb FROM sw.events
                WHERE run_id=:id AND event->>'type'='changes.undo.started'
                  AND NOT EXISTS (
                    SELECT 1 FROM sw.events e WHERE e.run_id=:id AND e.event->>'type'='changes.undo.completed'
                  )
                ORDER BY operation_id LIMIT 100
            """),
            {"id": run_id},
        ).mappings()
    ]


def claim(
    engine: Engine,
    tenant: str,
    owner: str,
    *,
    lease: Connection | None = None,
    one_per_member: bool = False,
) -> JSONDict | None:
    with tenant_scope(lease, tenant) if lease is not None else scoped(engine, tenant) as conn:
        row = (
            conn.execute(
                text("""
            SELECT r.id AS run_id,r.session_id,r.prompt,r.provider,r.mode,
                r.continuation,r.review,r.skills,r.undo,r.context_window_tokens,
                s.workspace_id,s.isolated,r.member_id::text AS member_id
            FROM sw.run_jobs j JOIN sw.runs r ON (j.tenant_id,j.run_id)=(r.tenant_id,r.id)
            JOIN sw.sessions s ON (r.tenant_id,r.session_id)=(s.tenant_id,s.id)
            WHERE j.state='queued' AND r.status='active' AND NOT r.cancel_requested
            AND (NOT :distributed OR r.member_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM sw.run_jobs running JOIN sw.runs active
                   ON (active.tenant_id,active.id)=(running.tenant_id,running.run_id)
                WHERE running.state='running' AND active.member_id=r.member_id
            ))
            ORDER BY j.queued_at,j.run_id LIMIT 1 FOR UPDATE OF r SKIP LOCKED
        """),
                {"distributed": one_per_member},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        attempt = conn.execute(
            text("""
            UPDATE sw.run_jobs SET state='running',owner=:owner,attempt=attempt+1,heartbeat_at=clock_timestamp()
            WHERE run_id=:id AND state='queued' RETURNING attempt
        """),
            {"id": row["run_id"], "owner": owner},
        ).scalar_one_or_none()
        if attempt is None:
            return None
        return {
            **row,
            **{key: str(row[key]) for key in ("run_id", "session_id", "workspace_id")},
            "attempt": attempt,
        }


def event_page(engine: Engine, tenant: str, run_id: str, after: int) -> list[JSONDict]:
    with scoped(engine, tenant) as conn:
        return list(
            conn.execute(
                text("""
            SELECT event FROM sw.events WHERE run_id=:id AND seq>:after ORDER BY seq LIMIT 128
        """),
                {"id": run_id, "after": after},
            ).scalars()
        )


def recent(
    engine: Engine, tenant: str, session: str | None = None, access: Access | None = None
) -> list[JSONDict]:
    with scoped(engine, tenant) as conn:
        if session is not None:
            turns = conn.execute(
                text(f"""
                    SELECT r.id AS run_id,r.session_id,r.status,r.prompt,r.created_at,r.mode,
                        report IS NOT NULL AS has_report,review IS NOT NULL AS is_review,r.skills,
                        outcome->>'final_text' AS final_text,
                        status='interrupted' OR COALESCE((outcome->>'unfinished_tools')::integer,0)>0
                            AS requires_acknowledgement
                    FROM sw.runs r JOIN sw.sessions s ON (r.tenant_id,r.session_id)=(s.tenant_id,s.id)
                    WHERE r.session_id=:session AND {_ACCESS}
                    ORDER BY r.created_at DESC,r.id DESC LIMIT 50
                """),
                {"session": session, **_visible(access)},
            ).mappings()
            return [
                {**row, "run_id": str(row["run_id"]), "session_id": str(row["session_id"])}
                for row in turns
            ]
        return [
            {**row, "run_id": str(row["run_id"]), "session_id": str(row["session_id"])}
            for row in conn.execute(
                text(f"""
                SELECT r.id AS run_id,r.session_id,r.status,left(r.prompt,120) AS prompt,r.created_at,
                    review IS NOT NULL AS is_review
                FROM sw.runs r JOIN sw.sessions s ON (r.tenant_id,r.session_id)=(s.tenant_id,s.id)
                WHERE {_ACCESS}
                ORDER BY r.created_at DESC,r.id DESC LIMIT 50
            """),
                _visible(access),
            ).mappings()
        ]


def recent_threads(engine: Engine, tenant: str, access: Access | None = None) -> list[JSONDict]:
    with scoped(engine, tenant) as conn:
        rows = conn.execute(
            text(f"""
            WITH latest AS (
                SELECT DISTINCT ON (session_id) tenant_id,id,session_id,status,created_at,
                    review IS NOT NULL AS is_review
                FROM sw.runs ORDER BY session_id,created_at DESC,id DESC
            )
            SELECT latest.id::text AS run_id,latest.session_id::text AS session_id,
                s.workspace_id::text AS workspace_id,latest.status,latest.is_review,
                s.archived_at IS NOT NULL AS archived,
                (SELECT left(r.prompt,120) FROM sw.runs r
                 WHERE (r.tenant_id,r.session_id)=(latest.tenant_id,latest.session_id)
                 ORDER BY r.created_at,r.id LIMIT 1) AS title,
                latest.status='active' AND EXISTS (
                    SELECT 1 FROM sw.approvals a WHERE (a.tenant_id,a.run_id)=(latest.tenant_id,latest.id)
                      AND a.state='pending'
                      AND a.expires_at>clock_timestamp()
                ) AS needs_approval
            FROM latest JOIN sw.sessions s ON (s.tenant_id,s.id)=(latest.tenant_id,latest.session_id)
            WHERE {_ACCESS}
            ORDER BY latest.created_at DESC,latest.id DESC
        """),
            _visible(access),
        ).mappings()
        return [dict(row) for row in rows]


def archive_thread(
    engine: Engine, tenant: str, session: str, access: Access, archive: bool
) -> bool:
    with scoped(engine, tenant) as conn:
        locked = conn.execute(
            text(f"""
            SELECT s.id FROM sw.sessions s WHERE s.id=:session AND {_ACCESS} FOR UPDATE
        """),
            {"session": session, **access.bindings()},
        ).scalar_one_or_none()
        if locked is None:
            return False
        if (
            archive
            and conn.execute(
                text("""
            SELECT EXISTS(SELECT 1 FROM sw.runs WHERE session_id=:session AND status='active')
        """),
                {"session": session},
            ).scalar_one()
        ):
            raise RunUnavailable()
        conn.execute(
            text("""
            UPDATE sw.sessions SET archived_at=CASE WHEN :archived THEN now() END WHERE id=:session
        """),
            {"session": session, "archived": archive},
        )
        return True
