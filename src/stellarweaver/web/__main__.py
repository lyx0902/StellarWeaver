"""Migration, loopback API, and local queue worker commands."""

import argparse
import json
import os
import re
import signal
from pathlib import Path, PureWindowsPath
from threading import Event
from urllib.parse import urlsplit
from uuid import UUID

import uvicorn
from sqlalchemy import URL
from sqlalchemy.exc import OperationalError

from stellarweaver.config import ModelConfig, get_api_key
from stellarweaver.web import db
from stellarweaver.web.app import LOCAL_TENANT, Principal, create_app
from stellarweaver.web.identity import Member, load_members
from stellarweaver.web.isolation import Isolation
from stellarweaver.web.local_accounts import LocalConfig, is_project
from stellarweaver.web.oidc import OIDC, OIDCConfig
from stellarweaver.web.worker import run_team_worker, run_worker


def configured_dsn(*, migrate: bool) -> str | None:
    name = "STELLARWEAVER_MIGRATION_DSN" if migrate else "STELLARWEAVER_DATABASE_DSN"
    dsn = os.environ.get(name)
    if dsn:
        return dsn
    host, password = os.environ.get("SW_WEB_DB_HOST"), os.environ.get("SW_WEB_DB_PASSWORD")
    if not host or not password:
        return None
    return URL.create(
        "postgresql+psycopg",
        username="stellarweaver_admin" if migrate else "stellarweaver_app",
        password=password,
        host=host,
        database="stellarweaver",
    ).render_as_string(hide_password=False)


def team_auth(
    members: tuple[Member, ...],
    roots: tuple[Path, ...],
    identity: str | None,
    local_owner: tuple[str, Principal] | None = None,
) -> tuple[dict[str, Principal], dict[str, Principal], OIDC | None]:
    bearers: dict[str, Principal] = dict((local_owner,)) if local_owner is not None else {}
    subjects: dict[str, Principal] = {}
    allow_test = os.getenv("SW_WEB_ALLOW_TEST_TOKENS") == "1"
    tokens = json.loads(os.getenv("SW_WEB_TEST_TOKENS") or "{}") if allow_test else {}
    issuer = os.getenv("SW_WEB_OIDC_ISSUER")
    local_http = os.getenv("SW_WEB_OIDC_LOOPBACK_HTTP") == "1"
    oidc = (
        OIDC(
            OIDCConfig(
                issuer,
                os.getenv("SW_WEB_OIDC_ORIGIN", ""),
                os.getenv("SW_WEB_OIDC_CLIENT_ID", ""),
                os.getenv("SW_WEB_OIDC_CLIENT_SECRET", ""),
                allow_local_http=local_http,
            )
        )
        if issuer
        else None
    )
    for member in members:
        granted = tuple(roots[i] for i in sorted(set(member.workspace_indexes)))
        account = Principal(member.tenant, granted, identity, member)
        if member.subject is not None and oidc is not None:
            subjects[member.subject] = account
        if allow_test and member.token_env:
            token = tokens.get(member.token_env) if isinstance(tokens, dict) else None
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
                or token in bearers
            ):
                raise ValueError("Missing, malformed or duplicate test account token")
            bearers[token] = account
    if bool(oidc) != bool(subjects) or not (subjects or bearers):
        raise ValueError("Configure OIDC identities, a local owner, or enabled local test accounts")
    return bearers, subjects, oidc


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="Use STELLARWEAVER_MIGRATION_DSN")
    serve = commands.add_parser("serve", help="Bind the workbench API to 127.0.0.1")
    worker = commands.add_parser("worker", help="Execute durable jobs from the local workbench")
    for command in (serve, worker):
        command.add_argument("--workspace", type=Path, action="append", required=True)
        command.add_argument("--state-dir", type=Path, default=Path(".stellarweaver/web-state"))
        command.add_argument("--provider", choices=("deepseek", "fake"), default="deepseek")
        command.add_argument("--env-file", type=Path)
        command.add_argument(
            "--enable-repair",
            action="store_true",
            default=os.getenv("SW_WEB_ENABLE_REPAIR") == "1",
            help="Allow approved edits and local unittest in trusted workspaces",
        )
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    serve.add_argument("--static-dir", type=Path, help="Serve a built workbench frontend")
    serve.add_argument(
        "--queued", action="store_true", help="Persist jobs for an independent worker"
    )
    worker.add_argument(
        "--once", action="store_true", help="Process up to one available job and exit"
    )
    args = parser.parse_args()
    if args.command == "migrate":
        dsn = configured_dsn(migrate=True)
        if not dsn:
            parser.error("Set STELLARWEAVER_MIGRATION_DSN")
        db.migrate(dsn)
        return
    roots = tuple(path.resolve(strict=True) for path in args.workspace)
    if not all(root.is_dir() for root in roots):
        parser.error("Each workspace must be a directory")
    identity = os.getenv("SW_WEB_WORKSPACE_IDENTITY")
    principal = Principal(LOCAL_TENANT, roots, identity)
    local = None
    try:
        isolation = Isolation.from_environment()
        members_file = os.getenv("SW_WEB_MEMBERS")
        members = load_members(Path(members_file), len(roots)) if members_file else ()
        if members and not isolation:
            raise ValueError("Team mode requires job isolation")
        if projects := os.getenv("SW_WEB_PROJECTS_ROOT"):
            host = os.environ["SW_WEB_PROJECTS_HOST_ROOT"]
            legacy = PureWindowsPath(identity or "").relative_to(PureWindowsPath(host)).as_posix()
            local = LocalConfig(
                Path(projects).resolve(strict=True),
                host,
                LOCAL_TENANT,
                UUID(os.environ["SW_WEB_LOCAL_OWNER_ID"]),
                next(iter(principal.workspace_map())),
                legacy,
            )
            if not isolation or not is_project(local.folder(legacy)):
                raise ValueError(
                    "Local accounts require the isolated worker and existing Git project"
                )
    except ValueError as exc:
        parser.error(str(exc))
    except (KeyError, OSError):
        parser.error("Could not load the configured account manifest or project root")
    if isolation and args.command == "serve" and not args.queued:
        parser.error("Isolated verification requires --queued and a G4 worker")
    if identity and len(roots) != 1:
        parser.error("A bound workspace identity requires one workspace")
    dsn = configured_dsn(migrate=False)
    token = os.environ.get("STELLARWEAVER_WEB_TOKEN")
    if not dsn:
        parser.error("Set STELLARWEAVER_DATABASE_DSN")
    if args.command == "serve" and not token and not members and local is None:
        parser.error("Set STELLARWEAVER_WEB_TOKEN")
    needs_key = args.provider == "deepseek" and (args.command == "worker" or not args.queued)
    key = (
        get_api_key(ModelConfig(provider="deepseek"), Path.cwd(), args.env_file)
        if needs_key
        else None
    )
    if needs_key and not key:
        parser.error(
            "DeepSeek key missing in launcher configuration; use --provider fake for the offline demo"
        )
    engine = db.engine_for(dsn)
    try:
        if args.command == "worker":
            shutdown = Event()

            def stop_worker(_: int, __: object) -> None:
                shutdown.set()

            previous_handler = signal.signal(signal.SIGTERM, stop_worker)
            try:
                if (members or local is not None) and isolation is not None:
                    endpoint = os.getenv("SW_WEB_EXECUTOR_URL", "")
                    secret = os.getenv("SW_WEB_EXECUTOR_TOKEN", "")
                    parsed = urlsplit(endpoint)
                    if (
                        parsed.scheme not in {"https", "http"}
                        or not parsed.hostname
                        or parsed.username
                        or parsed.password
                        or parsed.path
                        or parsed.query
                        or parsed.fragment
                        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret)
                    ):
                        parser.error("Team worker requires an executor URL and private token")
                    tenant_roots: dict[str, dict[str, Path]] = {}
                    for member in members if local is None else ():
                        granted = tuple(roots[i] for i in sorted(set(member.workspace_indexes)))
                        tenant_roots.setdefault(str(member.tenant), {}).update(
                            Principal(member.tenant, granted, identity).workspace_map()
                        )
                    run_team_worker(
                        engine,
                        tenant_roots,
                        args.state_dir.resolve(),
                        provider=args.provider,
                        api_key=key,
                        enable_repair=args.enable_repair,
                        isolation=isolation,
                        remote=(endpoint, secret),
                        slots=int(os.getenv("SW_WEB_EXECUTOR_SLOTS", "2")),
                        shutdown=shutdown,
                        once=args.once,
                        local=local,
                    )
                else:
                    run_worker(
                        engine,
                        str(LOCAL_TENANT),
                        principal.workspace_map(),
                        args.state_dir.resolve(),
                        provider=args.provider,
                        api_key=key,
                        enable_repair=args.enable_repair,
                        once=args.once,
                        shutdown=shutdown,
                        isolation=isolation,
                    )
            except KeyboardInterrupt:
                return
            except OperationalError:
                raise SystemExit("Worker database connection lost") from None
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
            return
        local_owner = None
        if local is not None:
            bearers: dict[str, Principal] = {}
            subjects: dict[str, Principal] = {}
            oidc = None
        elif members:
            try:
                if owner_id := os.getenv("SW_WEB_LOCAL_OWNER_ID"):
                    owner_uuid = UUID(owner_id)
                    owner_member = next(
                        (m for m in members if m.id == owner_uuid and m.tenant == LOCAL_TENANT),
                        None,
                    )
                    if not token or owner_member is None or owner_member.role != "admin":
                        raise ValueError(
                            "The local owner requires an existing token and local admin member"
                        )
                    granted = tuple(roots[i] for i in sorted(set(owner_member.workspace_indexes)))
                    local_owner = (token, Principal(LOCAL_TENANT, granted, identity, owner_member))
                bearers, subjects, oidc = team_auth(members, roots, identity, local_owner)
                if bearers and args.host != "127.0.0.1" and os.getenv("SW_WEB_LOCAL_PILOT") != "1":
                    raise ValueError("Team bearer accounts require the loopback-only profile")
            except ValueError as exc:
                parser.error(str(exc))
        else:
            assert token is not None
            bearers, subjects, oidc = {token: principal}, {}, None
        app = create_app(
            engine,
            bearers,
            args.state_dir.resolve(),
            provider=args.provider,
            api_key=key,
            enable_repair=args.enable_repair,
            queued=args.queued,
            static_dir=args.static_dir,
            isolated=isolation is not None,
            subjects=subjects,
            oidc=oidc,
            legacy_owner=local_owner[1] if local_owner else None,
            local=local,
        )
        uvicorn.run(
            app, host=args.host, port=args.port, proxy_headers=False, access_log=oidc is None
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
