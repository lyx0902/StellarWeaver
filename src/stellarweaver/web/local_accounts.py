"""Loopback account enrollment and tenant-scoped project selection."""

import hashlib
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from stellarweaver.domain import JSONDict
from stellarweaver.tools.workspace import Workspace
from stellarweaver.web import db
from stellarweaver.web.identity import Access

ENROLLMENT_LOCK = 1645207843


class AccountExists(Exception):
    pass


def account_name(value: str) -> tuple[str, str]:
    name = unicodedata.normalize("NFKC", value.strip())
    if not 3 <= len(name) <= 40 or not all(c.isalnum() or c in "._-" for c in name):
        raise ValueError("账号名须为 3–40 个字母、汉字、数字、点、下划线或横线。")
    return name, name.casefold()


def _digest(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()


@dataclass(frozen=True)
class LocalConfig:
    root: Path
    display_root: str
    legacy_tenant: UUID
    legacy_member: UUID
    legacy_workspace: str
    legacy_path: str

    def folder(self, relative: str) -> Path:
        parts = PurePosixPath(relative)
        if (
            parts.is_absolute()
            or any(p.startswith(".") for p in parts.parts)
            or "\\" in relative
            or ":" in relative
            or Workspace.is_protected(relative)
        ):
            raise ValueError("请选择本机项目目录中的文件夹。")
        path = self.root.joinpath(*parts.parts)
        if not path.is_dir() or path.resolve() != path:
            raise ValueError("项目目录不存在或不可访问。")
        return path

    def describe(self, relative: str) -> str:
        return self.display_root.rstrip("/\\") + ("/" + relative if relative else "")

    def folders(self, relative: str) -> JSONDict:
        path = self.folder(relative)
        entries = sorted(path.iterdir(), key=lambda child: child.name.casefold())
        return {
            "path": path.relative_to(self.root).as_posix() if path != self.root else "",
            "display_path": self.describe(relative),
            "selectable": is_project(path),
            "folders": [
                {"name": child.name, "path": child.relative_to(self.root).as_posix()}
                for child in entries
                if child.is_dir() and not child.is_symlink() and not child.name.startswith(".")
            ][:500],
        }


def is_project(path: Path) -> bool:
    git = path / ".git"
    return git.is_dir() and not git.is_symlink() and (git / "HEAD").is_file()


def chat_id(tenant: str) -> str:
    return str(uuid5(UUID(tenant), "personal-chat"))


def _add_workspace(
    conn: Connection, tenant: str, member: str, workspace: str, label: str, relative: str | None
) -> None:
    params = {"tenant": tenant, "actor": member, "id": workspace, "label": label, "path": relative}
    conn.execute(
        text("""
        INSERT INTO sw.workspaces(tenant_id,id,label) VALUES (:tenant,:id,:label)
        ON CONFLICT (tenant_id,id) DO NOTHING
    """),
        params,
    )
    conn.execute(
        text("""
        INSERT INTO sw.local_projects(tenant_id,workspace_id,relative_path) VALUES (:tenant,:id,:path)
        ON CONFLICT (tenant_id,workspace_id) DO NOTHING
    """),
        params,
    )
    conn.execute(
        text("""
        INSERT INTO sw.workspace_grants(tenant_id,member_id,workspace_id) VALUES (:tenant,:actor,:id)
        ON CONFLICT DO NOTHING
    """),
        params,
    )


def enroll(engine: Engine, config: LocalConfig, username: str, password: str) -> tuple[str, str]:
    name, key = account_name(username)
    salt = secrets.token_bytes(16)
    stored = salt.hex() + ":" + _digest(password, salt)
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": ENROLLMENT_LOCK})
        existing = conn.execute(
            text("SELECT 1 FROM sw.local_accounts WHERE username_key=:key"), {"key": key}
        ).scalar_one_or_none()
        if existing:
            raise AccountExists()
        first = not conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM sw.local_accounts)")
        ).scalar_one()
        tenant = str(config.legacy_tenant if first else uuid4())
        member = str(config.legacy_member if first else uuid4())
        params = {"tenant": tenant, "member": member, "name": name, "key": key, "hash": stored}
        conn.execute(text("SELECT set_config('sw.tenant_id', :tenant, true)"), params)
        conn.execute(text("INSERT INTO sw.tenants VALUES (:tenant) ON CONFLICT DO NOTHING"), params)
        conn.execute(
            text("""
            INSERT INTO sw.memberships(tenant_id,id,name,role,active_limit,daily_tokens)
            VALUES(:tenant,:member,:name,'admin',3,500000)
            ON CONFLICT (tenant_id,id) DO UPDATE SET name=excluded.name
        """),
            params,
        )
        conn.execute(
            text("""
            INSERT INTO sw.local_accounts VALUES(:key,:name,:hash,:tenant,:member)
        """),
            params,
        )
        _add_workspace(conn, tenant, member, chat_id(tenant), "普通对话", None)
        if first:
            _add_workspace(
                conn,
                tenant,
                member,
                config.legacy_workspace,
                PurePosixPath(config.legacy_path).name,
                config.legacy_path,
            )
            db.assign_legacy_threads(conn, Access((config.legacy_workspace,), member))
    return tenant, member


def authenticate(engine: Engine, username: str, password: str) -> tuple[str, str] | None:
    _, key = account_name(username)
    with engine.connect() as conn:
        row = conn.execute(
            text("""
            SELECT tenant_id::text,member_id::text,password_hash FROM sw.local_accounts WHERE username_key=:key
        """),
            {"key": key},
        ).one_or_none()
    salt, expected = row[2].split(":") if row else ("00" * 16, "00" * 64)
    if secrets.compare_digest(_digest(password, bytes.fromhex(salt)), expected) and row is not None:
        return row[0], row[1]
    return None


def lookup(engine: Engine, tenant: str, member: str) -> str | None:
    with engine.connect() as conn:
        result: str | None = conn.execute(
            text("""
            SELECT username FROM sw.local_accounts WHERE tenant_id=:tenant AND member_id=:member
        """),
            {"tenant": tenant, "member": member},
        ).scalar_one_or_none()
        return result


def tenants(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT tenant_id::text FROM sw.local_accounts ORDER BY tenant_id")
            ).scalars()
        )


def projects(engine: Engine, tenant: str, member: str) -> list[JSONDict]:
    with db.scoped(engine, tenant) as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("""
            SELECT w.id::text,COALESCE(p.alias,w.label) AS label,w.label AS default_label,
                   p.alias,p.relative_path FROM sw.local_projects p
            JOIN sw.workspaces w ON (w.tenant_id,w.id)=(p.tenant_id,p.workspace_id)
            JOIN sw.workspace_grants g ON (g.tenant_id,g.workspace_id)=(p.tenant_id,p.workspace_id)
            WHERE g.member_id=:member ORDER BY (p.relative_path IS NULL),lower(w.label),w.id
        """),
                {"member": member},
            ).mappings()
        ]


def project_for_job(engine: Engine, tenant: str, member: str, workspace: str) -> JSONDict | None:
    return next((row for row in projects(engine, tenant, member) if row["id"] == workspace), None)


def rename_project(
    engine: Engine, tenant: str, member: str, workspace: str, alias: str | None
) -> JSONDict | None:
    with db.scoped(engine, tenant) as conn:
        row = (
            conn.execute(
                text("""
                UPDATE sw.local_projects AS p SET alias=:alias
                FROM sw.workspaces AS w, sw.workspace_grants AS g
                WHERE p.tenant_id=:tenant AND p.workspace_id=:workspace
                  AND p.relative_path IS NOT NULL
                  AND (w.tenant_id,w.id)=(p.tenant_id,p.workspace_id)
                  AND (g.tenant_id,g.workspace_id)=(p.tenant_id,p.workspace_id)
                  AND g.member_id=:member
                RETURNING p.alias,COALESCE(p.alias,w.label) AS label
            """),
                {"tenant": tenant, "member": member, "workspace": workspace, "alias": alias},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


def add_project(engine: Engine, config: LocalConfig, tenant: str, member: str, path: str) -> None:
    folder = config.folder(path)
    if not is_project(folder):
        raise ValueError("请选择包含 Git 仓库的项目文件夹。")
    relative = folder.relative_to(config.root).as_posix()
    with db.scoped(engine, tenant) as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:tenant), 6)"), {"tenant": tenant})
        workspace = conn.execute(
            text("""
            SELECT workspace_id::text FROM sw.local_projects WHERE relative_path=:path
        """),
            {"path": relative},
        ).scalar_one_or_none() or str(uuid5(UUID(tenant), "project:" + relative))
        _add_workspace(conn, tenant, member, workspace, folder.name, relative)
