"""Task workspace identity and explicit Web follow-up/review requests."""

from alembic import op

revision = "g4_threads"
down_revision = "g3_run_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE sw.sessions ADD COLUMN isolated boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE sw.runs ADD COLUMN continuation text, ADD COLUMN review jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE sw.runs DROP COLUMN review, DROP COLUMN continuation")
    op.execute("ALTER TABLE sw.sessions DROP COLUMN isolated")
