"""Membership, per-project grants, audit, quota accounting, and browser sessions."""

from alembic import op

revision = "g5_identity"
down_revision = "g4_threads"
branch_labels = None
depends_on = None

_TABLES = {
    "memberships": """
        tenant_id uuid NOT NULL REFERENCES sw.tenants, id uuid NOT NULL, name text NOT NULL,
        role text NOT NULL CHECK(role IN ('admin','member','viewer')),
        active_limit integer NOT NULL CHECK(active_limit>0),
        daily_tokens bigint NOT NULL CHECK(daily_tokens>0),
        PRIMARY KEY(tenant_id,id)""",
    "workspace_grants": """
        tenant_id uuid NOT NULL, member_id uuid NOT NULL, workspace_id uuid NOT NULL,
        PRIMARY KEY(tenant_id,member_id,workspace_id),
        FOREIGN KEY(tenant_id,member_id) REFERENCES sw.memberships,
        FOREIGN KEY(tenant_id,workspace_id) REFERENCES sw.workspaces""",
    "audit": """
        tenant_id uuid NOT NULL, id bigint GENERATED ALWAYS AS IDENTITY,
        member_id uuid NOT NULL, run_id uuid NOT NULL, operation_id uuid,
        action text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,member_id) REFERENCES sw.memberships,
        FOREIGN KEY(tenant_id,run_id) REFERENCES sw.runs""",
    "model_usage": """
        tenant_id uuid NOT NULL, run_id uuid NOT NULL, request_index integer NOT NULL,
        member_id uuid NOT NULL, usage_day date NOT NULL DEFAULT ((now() AT TIME ZONE 'UTC')::date),
        tokens bigint NOT NULL CHECK(tokens>=0), settled boolean NOT NULL DEFAULT false,
        estimated boolean NOT NULL DEFAULT true,
        PRIMARY KEY(tenant_id,run_id,request_index),
        FOREIGN KEY(tenant_id,run_id) REFERENCES sw.runs,
        FOREIGN KEY(tenant_id,member_id) REFERENCES sw.memberships""",
    "browser_sessions": """
        tenant_id uuid NOT NULL, digest text NOT NULL, member_id uuid NOT NULL,
        expires_at timestamptz NOT NULL,
        PRIMARY KEY(tenant_id,digest), FOREIGN KEY(tenant_id,member_id) REFERENCES sw.memberships""",
}


def upgrade() -> None:
    for table, columns in _TABLES.items():
        op.execute(f"CREATE TABLE sw.{table} ({columns})")
        op.execute(f"ALTER TABLE sw.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE sw.{table} FORCE ROW LEVEL SECURITY")
        check = "tenant_id = NULLIF(current_setting('sw.tenant_id', true), '')::uuid"
        op.execute(f"CREATE POLICY tenant_scope ON sw.{table} USING ({check}) WITH CHECK ({check})")
        op.execute(f"GRANT SELECT, INSERT ON sw.{table} TO stellarweaver_app")
    op.execute("GRANT UPDATE ON sw.memberships,sw.model_usage TO stellarweaver_app")
    op.execute("GRANT DELETE ON sw.workspace_grants,sw.browser_sessions TO stellarweaver_app")
    op.execute("GRANT USAGE ON SEQUENCE sw.audit_id_seq TO stellarweaver_app")
    for table in ("sessions", "runs"):
        op.execute(
            f"ALTER TABLE sw.{table} ADD COLUMN member_id uuid, "
            "ADD FOREIGN KEY(tenant_id,member_id) REFERENCES sw.memberships"
        )
    op.execute("CREATE INDEX member_runs ON sw.runs(tenant_id,member_id,status)")
    op.execute("CREATE INDEX usage_by_day ON sw.model_usage(tenant_id,member_id,usage_day)")
    op.execute("CREATE INDEX runs_by_thread ON sw.runs(tenant_id,session_id,created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX sw.runs_by_thread")
    op.execute("DROP INDEX sw.member_runs")
    for table in ("runs", "sessions"):
        op.execute(f"ALTER TABLE sw.{table} DROP COLUMN member_id")
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE sw.{table}")
