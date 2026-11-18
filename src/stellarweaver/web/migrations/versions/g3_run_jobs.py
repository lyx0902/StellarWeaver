"""Durable workbench run queue."""

from alembic import op

revision = "g3_run_jobs"
down_revision = "g2_repair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE sw.run_jobs (
            tenant_id uuid, run_id uuid,
            state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','done')),
            queued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            owner uuid, attempt integer NOT NULL DEFAULT 0,
            heartbeat_at timestamptz,
            PRIMARY KEY (tenant_id,run_id),
            FOREIGN KEY (tenant_id,run_id) REFERENCES sw.runs,
            CHECK ((state = 'queued' AND owner IS NULL AND attempt = 0) OR
                   (state = 'running' AND owner IS NOT NULL AND attempt > 0) OR state = 'done')
        )
    """)
    op.execute("ALTER TABLE sw.run_jobs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sw.run_jobs FORCE ROW LEVEL SECURITY")
    check = "tenant_id = NULLIF(current_setting('sw.tenant_id', true), '')::uuid"
    op.execute(f"CREATE POLICY tenant_scope ON sw.run_jobs USING ({check}) WITH CHECK ({check})")
    op.execute("GRANT SELECT, INSERT, UPDATE ON sw.run_jobs TO stellarweaver_app")
    op.execute("CREATE INDEX run_jobs_queue ON sw.run_jobs(tenant_id,state,queued_at,run_id)")


def downgrade() -> None:
    op.execute("DROP TABLE sw.run_jobs")
