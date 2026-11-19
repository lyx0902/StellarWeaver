"""Keep model request timestamps for local-day reporting."""

from alembic import op

revision = "g7_model_usage_time"
down_revision = "g6_local_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE sw.model_usage ADD COLUMN started_at timestamptz")
    op.execute("""
        WITH starts AS (
            SELECT tenant_id,run_id,event->'payload'->>'request_index' AS request_index,
                   min((event->>'timestamp')::timestamptz) AS started_at
            FROM sw.events
            WHERE event->>'type'='model.started'
              AND pg_input_is_valid(event->>'timestamp', 'timestamp with time zone')
            GROUP BY tenant_id,run_id,event->'payload'->>'request_index'
        )
        UPDATE sw.model_usage u SET started_at=s.started_at
        FROM starts s WHERE (u.tenant_id,u.run_id,u.request_index::text) =
                            (s.tenant_id,s.run_id,s.request_index)
    """)
    op.execute("ALTER TABLE sw.model_usage ALTER COLUMN started_at SET DEFAULT now()")
    op.execute("CREATE INDEX usage_by_started_at ON sw.model_usage(tenant_id,started_at)")


def downgrade() -> None:
    op.execute("ALTER TABLE sw.model_usage DROP COLUMN started_at")
