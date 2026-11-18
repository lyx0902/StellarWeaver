"""Approval and delivery evidence for the opt-in repair workflow."""

from alembic import op

revision = "g2_repair"
down_revision = "g1_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sw.runs ADD COLUMN mode text NOT NULL DEFAULT 'read-only' CHECK (mode IN ('read-only','repair'))"
    )
    op.execute("ALTER TABLE sw.runs ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE sw.runs ADD COLUMN report jsonb")
    op.execute("""
        CREATE TABLE sw.approvals (
            tenant_id uuid, run_id uuid, id uuid,
            state text NOT NULL CHECK(state IN ('pending','approved','denied','consumed','expired','cancelled')),
            expires_at timestamptz NOT NULL, preview jsonb NOT NULL, fingerprint text NOT NULL,
            PRIMARY KEY (tenant_id,run_id,id),
            FOREIGN KEY (tenant_id,run_id,id) REFERENCES sw.operations
        )
    """)
    op.execute("ALTER TABLE sw.approvals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sw.approvals FORCE ROW LEVEL SECURITY")
    check = "tenant_id = NULLIF(current_setting('sw.tenant_id', true), '')::uuid"
    op.execute(f"CREATE POLICY tenant_scope ON sw.approvals USING ({check}) WITH CHECK ({check})")
    op.execute("GRANT SELECT, INSERT, UPDATE ON sw.approvals TO stellarweaver_app")


def downgrade() -> None:
    op.execute("DROP TABLE sw.approvals")
    op.execute(
        "ALTER TABLE sw.runs DROP COLUMN mode, DROP COLUMN cancel_requested, DROP COLUMN report"
    )
