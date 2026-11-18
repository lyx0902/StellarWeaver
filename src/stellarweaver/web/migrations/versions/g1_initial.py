"""G1 read-only web journal and tenant constraints."""

from alembic import op

revision = "g1_initial"
down_revision = None
branch_labels = None
depends_on = None

_TABLES = {
    "tenants": "tenant_id uuid PRIMARY KEY",
    "workspaces": """
        tenant_id uuid REFERENCES sw.tenants, id uuid, label text NOT NULL,
        PRIMARY KEY(tenant_id,id)""",
    "sessions": """
        tenant_id uuid, id uuid, workspace_id uuid NOT NULL, next_seq bigint NOT NULL DEFAULT 0,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(tenant_id,id),
        FOREIGN KEY(tenant_id,workspace_id) REFERENCES sw.workspaces""",
    "runs": """
        tenant_id uuid, id uuid, session_id uuid NOT NULL, request_key uuid NOT NULL,
        prompt text NOT NULL, provider text NOT NULL, status text NOT NULL DEFAULT 'active',
        outcome jsonb, created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,request_key), UNIQUE(tenant_id,id,session_id),
        FOREIGN KEY(tenant_id,session_id) REFERENCES sw.sessions""",
    "messages": """
        tenant_id uuid, run_id uuid, id uuid, position bigint GENERATED ALWAYS AS IDENTITY,
        body jsonb NOT NULL, PRIMARY KEY(tenant_id,run_id,id),
        FOREIGN KEY(tenant_id,run_id) REFERENCES sw.runs""",
    "events": """
        tenant_id uuid, session_id uuid, run_id uuid NOT NULL, seq bigint,
        event jsonb NOT NULL, PRIMARY KEY(tenant_id,session_id,seq),
        FOREIGN KEY(tenant_id,run_id,session_id) REFERENCES sw.runs(tenant_id,id,session_id)""",
    "operations": """
        tenant_id uuid, run_id uuid, id uuid, assistant_id uuid NOT NULL,
        call_id text NOT NULL, tool_name text NOT NULL, args text NOT NULL,
        state text NOT NULL DEFAULT 'planned', decision jsonb, intent jsonb,
        result jsonb, result_event jsonb,
        PRIMARY KEY(tenant_id,run_id,id), UNIQUE(tenant_id,run_id,assistant_id,call_id),
        FOREIGN KEY(tenant_id,run_id,assistant_id) REFERENCES sw.messages""",
    "checkpoints": """
        tenant_id uuid, run_id uuid, id uuid, through_seq bigint NOT NULL, summary jsonb NOT NULL,
        PRIMARY KEY(tenant_id,run_id,id), FOREIGN KEY(tenant_id,run_id) REFERENCES sw.runs""",
}


def upgrade() -> None:
    op.execute("CREATE SCHEMA sw")
    for table, columns in _TABLES.items():
        op.execute(f"CREATE TABLE sw.{table} ({columns})")
        op.execute(f"ALTER TABLE sw.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE sw.{table} FORCE ROW LEVEL SECURITY")
        check = "tenant_id = NULLIF(current_setting('sw.tenant_id', true), '')::uuid"
        op.execute(f"CREATE POLICY tenant_scope ON sw.{table} USING ({check}) WITH CHECK ({check})")
    op.execute("CREATE INDEX events_run_cursor ON sw.events(tenant_id,run_id,seq)")
    op.execute("CREATE INDEX runs_recent ON sw.runs(tenant_id,created_at DESC)")
    op.execute("GRANT USAGE ON SCHEMA sw TO stellarweaver_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA sw TO stellarweaver_app")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA sw TO stellarweaver_app")


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP TABLE sw.{table}")
    op.execute("DROP SCHEMA sw")
