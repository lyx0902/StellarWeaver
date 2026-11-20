"""Store an optional display alias on existing local projects."""

from alembic import op

revision = "g8_workspace_alias"
down_revision = "g7_model_usage_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE sw.local_projects ADD COLUMN alias varchar(80),
        ADD CONSTRAINT local_project_alias_check
            CHECK (alias IS NULL OR
                   (relative_path IS NOT NULL AND alias <> '' AND alias = btrim(alias)))
    """)
    op.execute("GRANT UPDATE (alias) ON sw.local_projects TO stellarweaver_app")


def downgrade() -> None:
    op.execute("REVOKE UPDATE (alias) ON sw.local_projects FROM stellarweaver_app")
    op.execute("ALTER TABLE sw.local_projects DROP COLUMN alias")
