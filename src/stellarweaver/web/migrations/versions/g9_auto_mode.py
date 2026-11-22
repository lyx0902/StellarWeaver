"""Allow the auto authorization mode for project runs."""

from alembic import op

revision = "g9_auto_mode"
down_revision = "g8_workspace_alias"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sw.runs DROP CONSTRAINT runs_mode_check,
        ADD CONSTRAINT runs_mode_check CHECK (mode IN ('read-only', 'repair', 'auto'))
    """)


def downgrade() -> None:
    op.execute("UPDATE sw.runs SET mode = 'repair' WHERE mode = 'auto'")
    op.execute("""
        ALTER TABLE sw.runs DROP CONSTRAINT runs_mode_check,
        ADD CONSTRAINT runs_mode_check CHECK (mode IN ('read-only', 'repair'))
    """)
