"""Record workbench thread deletion and its durable cleanup progress."""

from alembic import op

revision = "g12_thread_deletion"
down_revision = "g11_context_window"
branch_labels = None
depends_on = None

_CONTENT_TABLES = "sw.approvals, sw.operations, sw.checkpoints, sw.events, sw.messages, sw.run_jobs"


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sw.sessions
        ADD COLUMN deleted_at timestamptz,
        ADD COLUMN purge_after timestamptz,
        ADD COLUMN purged_at timestamptz
    """)
    op.execute("""
        CREATE INDEX pending_session_cleanup ON sw.sessions(tenant_id, purge_after, id)
        WHERE deleted_at IS NOT NULL AND purged_at IS NULL
    """)
    op.execute(f"GRANT DELETE ON {_CONTENT_TABLES} TO stellarweaver_app")


def downgrade() -> None:
    op.execute(f"REVOKE DELETE ON {_CONTENT_TABLES} FROM stellarweaver_app")
    op.execute("DROP INDEX sw.pending_session_cleanup")
    op.execute("""
        ALTER TABLE sw.sessions
        DROP COLUMN purged_at, DROP COLUMN purge_after, DROP COLUMN deleted_at
    """)
