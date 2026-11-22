"""Persist skill selections, queued undo requests, and weekly quotas."""

from alembic import op

revision = "g10_workbench_parity"
down_revision = "g9_auto_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE sw.memberships RENAME COLUMN daily_tokens TO weekly_tokens")
    op.execute("ALTER TABLE sw.memberships NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE sw.memberships SET weekly_tokens=10000000 WHERE weekly_tokens=500000")
    op.execute("ALTER TABLE sw.memberships FORCE ROW LEVEL SECURITY")
    op.execute("""
        ALTER TABLE sw.runs
          ADD COLUMN skills jsonb NOT NULL DEFAULT '[]',
          ADD COLUMN undo jsonb
    """)
    op.execute(
        "ALTER TABLE sw.workspace_grants ADD COLUMN project_skills boolean NOT NULL DEFAULT false"
    )
    op.execute("GRANT UPDATE (project_skills) ON sw.workspace_grants TO stellarweaver_app")


def downgrade() -> None:
    op.execute("ALTER TABLE sw.workspace_grants DROP COLUMN project_skills")
    op.execute("ALTER TABLE sw.runs DROP COLUMN undo, DROP COLUMN skills")
    op.execute("ALTER TABLE sw.memberships RENAME COLUMN weekly_tokens TO daily_tokens")
