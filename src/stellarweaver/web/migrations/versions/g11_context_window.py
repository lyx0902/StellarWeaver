"""Pin the submitted context window and recover legacy values from model events."""

from alembic import op

revision = "g11_context_window"
down_revision = "g10_workbench_parity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sw.runs ADD COLUMN context_window_tokens integer
            CHECK (context_window_tokens >= 2048)
    """)
    op.execute("ALTER TABLE sw.runs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sw.events NO FORCE ROW LEVEL SECURITY")
    op.execute("""
        WITH observed AS (
            SELECT tenant_id,run_id,seq,
                CASE WHEN pg_input_is_valid(event->'payload'->>'context_window_tokens', 'integer')
                     THEN (event->'payload'->>'context_window_tokens')::integer
                END AS context_window_tokens
            FROM sw.events
            WHERE event->>'type'='model.started'
              AND (event->'payload'->>'purpose') IS DISTINCT FROM 'ds_search'
        ), first_request AS (
            SELECT DISTINCT ON (tenant_id,run_id) tenant_id,run_id,context_window_tokens
            FROM observed WHERE context_window_tokens>=2048
            ORDER BY tenant_id,run_id,seq
        )
        UPDATE sw.runs r SET context_window_tokens=first_request.context_window_tokens
        FROM first_request WHERE (r.tenant_id,r.id)=(first_request.tenant_id,first_request.run_id)
    """)
    op.execute("ALTER TABLE sw.events FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sw.runs FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE sw.runs DROP COLUMN context_window_tokens")
