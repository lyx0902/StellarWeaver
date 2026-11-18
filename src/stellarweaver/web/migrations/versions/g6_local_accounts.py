"""Local credentials, project registrations and thread archives."""

from alembic import op

revision = "g6_local_accounts"
down_revision = "g5_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This credential index is used before a tenant is known. Project data remains tenant-scoped.
    op.execute("""
        CREATE TABLE sw.local_accounts (
            username_key text PRIMARY KEY, username text NOT NULL, password_hash text NOT NULL,
            tenant_id uuid NOT NULL, member_id uuid NOT NULL UNIQUE,
            UNIQUE(tenant_id), FOREIGN KEY(tenant_id,member_id) REFERENCES sw.memberships
        )
    """)
    op.execute("GRANT SELECT, INSERT ON sw.local_accounts TO stellarweaver_app")
    op.execute("""
        CREATE TABLE sw.local_projects (
            tenant_id uuid NOT NULL, workspace_id uuid NOT NULL, relative_path text,
            PRIMARY KEY(tenant_id,workspace_id), UNIQUE(tenant_id,relative_path),
            FOREIGN KEY(tenant_id,workspace_id) REFERENCES sw.workspaces
        )
    """)
    op.execute("ALTER TABLE sw.local_projects ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sw.local_projects FORCE ROW LEVEL SECURITY")
    check = "tenant_id = NULLIF(current_setting('sw.tenant_id', true), '')::uuid"
    op.execute(
        f"CREATE POLICY tenant_scope ON sw.local_projects USING ({check}) WITH CHECK ({check})"
    )
    op.execute("GRANT SELECT, INSERT ON sw.local_projects TO stellarweaver_app")
    op.execute("ALTER TABLE sw.sessions ADD COLUMN archived_at timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE sw.sessions DROP COLUMN archived_at")
    op.execute("DROP TABLE sw.local_projects")
    op.execute("DROP TABLE sw.local_accounts")
