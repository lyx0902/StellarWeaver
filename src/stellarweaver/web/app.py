"""Loopback workbench with read-only runs and opt-in local repair."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid5

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from stellarweaver.domain import JSONDict
from stellarweaver.policy import DS_SEARCH
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web import db, local_accounts
from stellarweaver.web.approval import (
    DecisionConflict,
    decide,
    pending,
    request_cancel,
)
from stellarweaver.web.execution import execute, web_model_config
from stellarweaver.web.experiment_records import (
    ExperimentTooLarge,
    PublishedExperiments,
    read_legacy,
    redact,
)
from stellarweaver.web.identity import Access, Member
from stellarweaver.web.journal import PgJournal
from stellarweaver.web.oidc import (
    CALLBACK,
    FLOW_COOKIE,
    FLOW_SECONDS,
    OIDC,
    SESSION_COOKIE,
    SESSION_SECONDS,
    LoginRejected,
    create_session,
    logout,
    session_member,
)
from stellarweaver.web.review import feedback_prompt, preflight_patch, selected_patch
from stellarweaver.web.run_export import ExportLimitExceeded
from stellarweaver.web.run_export import build as build_export
from stellarweaver.web.run_export import render as render_export
from stellarweaver.web.skills import SkillLibrary, project_skill_root
from stellarweaver.web.undo import latest_edit
from stellarweaver.web.worker import recover_interrupted

LOCAL_TENANT = UUID("408f4403-a999-48de-94dc-cc049f8a95fd")


@dataclass(frozen=True)
class Principal:
    tenant: UUID
    workspaces: tuple[Path, ...]
    workspace_identity: str | None = None
    member: Member | None = None
    local_projects: tuple[JSONDict, ...] | None = None

    def workspace_map(self) -> dict[str, Path]:
        if self.workspace_identity is not None:
            (path,) = self.workspaces
            return {str(uuid5(self.tenant, "bind:" + self.workspace_identity)): path}
        return {str(uuid5(self.tenant, str(path))): path for path in self.workspaces}

    def workspace_label(self, path: Path) -> str:
        if self.workspace_identity:
            return Path(self.workspace_identity.replace("\\", "/")).name or "Workspace"
        return path.name or "Workspace"

    @property
    def member_id(self) -> str | None:
        return str(self.member.id) if self.member else None

    @property
    def access(self) -> Access | None:
        if self.member is None:
            return None
        return Access(
            tuple(self.workspace_map())
            if self.local_projects is None
            else tuple(project["id"] for project in self.local_projects),
            str(self.member.id),
            self.member_id if self.member.role == "member" else None,
        )

    @property
    def can_write(self) -> bool:
        return self.member is None or self.member.role != "viewer"


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: Annotated[str, Field(min_length=3, max_length=80)]
    password: Annotated[str, Field(min_length=8, max_length=128, repr=False)]


class ProjectFolder(BaseModel):
    path: Annotated[str, Field(max_length=600)]


class ArchiveThread(BaseModel):
    archived: bool


class SkillRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class SkillTrust(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


class UndoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    operation_id: UUID


class Submit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    request_id: UUID
    prompt: Annotated[str, Field(min_length=1, max_length=8000)]
    mode: db.RunMode = "read-only"
    session_id: UUID | None = None
    continuation: Literal["history", "fresh"] = "history"
    acknowledge_interrupted: bool = False
    skills: Annotated[list[SkillRef], Field(max_length=3)] | None = None


HunkId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class PatchSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hunks: Annotated[list[HunkId], Field(min_length=1, max_length=300)]


class ReviewSelection(PatchSelection):
    request_id: UUID


class ReviewFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    path: Annotated[str, Field(min_length=1, max_length=4096, strict=True)]
    hunk: HunkId | None = None
    text: Annotated[str, Field(min_length=1, max_length=2000)]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "deny"]


class WorkspaceAlias(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    alias: Annotated[str, Field(max_length=80, pattern=r"^[^\r\n]*$")]


def public_event(event: JSONDict) -> JSONDict:
    """Use an explicit web projection; internal journal entries stay intact."""
    kind, payload = event["type"], event["payload"]
    if kind == "assistant.completed":
        visible = {
            "content": payload.get("content", ""),
            "has_tools": bool(payload.get("tool_calls")),
        }
    elif kind == "assistant.delta":
        visible = {"content": payload["content"], "request_index": payload["request_index"]}
    elif kind in {
        "review.applying",
        "review.applied",
        "verification.started",
        "verification.completed",
        "run.recovery_context",
    }:
        visible = payload
    elif kind == "tool.reconciled":
        visible = {
            k: payload[k] for k in ("operation_id", "tool_name", "status", "reason") if k in payload
        }
    elif kind == "skill.selected":
        visible = {
            "skill_name": payload["name"],
            "skill_source": payload["source_kind"],
            "sha256": payload["sha256"],
        }
    elif kind == "context.prior_evidence":
        visible = {key: len(payload[key]) for key in ("files", "checks", "skills")}
    elif kind == "context.compacted":
        visible = {
            key: payload[key]
            for key in (
                "strategy",
                "before_tokens_estimated",
                "after_tokens_estimated",
                "input_budget",
            )
            if key in payload
        }
    elif kind in {"changes.undo.started", "changes.undo.completed"}:
        visible = {k: payload[k] for k in ("path", "target_operation_id", "status") if k in payload}
    elif kind == "tool.completed":
        visible = {k: payload[k] for k in ("tool_name", "status", "operation_id") if k in payload}
        if payload.get("status") == "ok":
            tool_name = payload.get("tool_name")
            if tool_name == "load_skill":
                data = payload.get("data", {})
                visible["skill_name"] = data.get("name")
                visible["skill_source"] = data.get("source_kind")
            else:
                visible["content"] = str(payload.get("content", ""))[:12000]
            if tool_name == DS_SEARCH:
                visible["sources"] = payload.get("data", {}).get("sources", [])
            if tool_name == "stage_patch":
                visible["staged_patch"] = payload.get("data", {}).get("patch")
                visible["patch_sha256"] = payload.get("data", {}).get("patch_sha256")
        else:
            visible["code"] = payload.get("data", {}).get("code")
            visible["exit_code"] = payload.get("data", {}).get("exit_code")
    elif kind in {"model.started", "model.completed"}:
        visible = {
            key: payload[key]
            for key in (
                "request_index",
                "context_window_tokens",
                "input_tokens",
                "input_estimated",
                "estimated",
                "purpose",
            )
            if key in payload
        }
    elif kind == "model.failed":
        visible = {"code": "MODEL_REQUEST_FAILED"}
        for key in ("status_code", "request_index", "retryable"):
            if key in payload:
                visible[key] = payload[key]
    elif kind == "repair.diagnostic":
        visible = {
            "code": payload["category"],
            "tool_name": payload["tool"],
            "operation_id": payload["operation_id"],
            "exit_code": payload.get("exit_code"),
            "content": payload.get("next_step", ""),
            "repeat_count": payload.get("repeat_count"),
        }
    elif kind == "harness.profile":
        visible = {
            key: payload[key]
            for key in (
                "development_tools",
                "issue_navigation",
                "repair_controller",
                "patch_staging",
                "task_evidence",
                "budget_guidance",
            )
            if key in payload
        }
    elif kind == "budget.guidance":
        visible = {
            "content": f"{payload['phase']} · 剩余工具调用 {payload['tool_calls_remaining']}",
            "code": "BUDGET_LOW" if payload["capacity_limited"] else None,
        }
    elif kind == "task.evidence":
        visible = {
            "content": f"{len(payload['files'])} 个已复核文件 · {len(payload['observations'])} 条工具证据",
        }
    elif kind in {
        "approval.requested",
        "approval.decided",
        "approval.expired",
        "approval.consumed",
        "approval.invalidated",
    }:
        visible = payload
    elif kind.startswith("run."):
        visible = {
            k: payload[k]
            for k in (
                "task",
                "run_status",
                "verification_status",
                "model_calls",
                "tool_calls",
                "duration_ms",
                "error_code",
                "failure_phase",
                "unfinished_tools",
                "recorded_results",
            )
            if k in payload
        }
        reason = str(payload.get("reason", ""))
        if payload.get("run_status") == "budget_exceeded" and reason.startswith(
            (
                "Required context does not fit:",
                "Server context length exceeded;",
                "Output reserve and safety margin",
                "context_window_tokens",
            )
        ):
            visible["error_code"] = "CONTEXT_WINDOW_EXCEEDED"
            visible["budget_reason"] = reason[:300]
    else:
        visible = {}
    return {**event, "payload": visible}


def create_app(
    engine: Engine,
    principals: Mapping[str, Principal],
    state_dir: Path,
    *,
    provider: str = "fake",
    api_key: str | None = None,
    enable_repair: bool = False,
    approval_ttl: float = 90,
    queued: bool = False,
    static_dir: Path | None = None,
    isolated: bool = False,
    subjects: Mapping[str, Principal] | None = None,
    oidc: OIDC | None = None,
    legacy_owner: Principal | None = None,
    local: local_accounts.LocalConfig | None = None,
    skill_roots: Sequence[tuple[str, Path]] = (),
    experiment_file: Path | None = None,
) -> FastAPI:
    identities = (*principals.values(), *(subjects or {}).values())
    if isolated and not queued:
        raise ValueError("Task isolation requires a queue worker")
    if (not identities and local is None) or any(
        len(token) < 32 or not token.isascii() for token in principals
    ):
        raise ValueError("Configure an ASCII web token of at least 32 characters")
    if (local is not None or any(p.member for p in identities)) and not (isolated and queued):
        raise ValueError("Shared accounts require queued and isolated execution")
    if bool(oidc) != bool(subjects):
        raise ValueError("OIDC requires a configured identity allowlist")
    if legacy_owner is not None and (
        legacy_owner not in principals.values()
        or legacy_owner.tenant != LOCAL_TENANT
        or legacy_owner.member is None
        or legacy_owner.member.role != "admin"
    ):
        raise ValueError("Legacy threads require the authenticated local administrator")
    if provider not in {"fake", "deepseek"} or (
        not queued and provider == "deepseek" and not api_key
    ):
        raise ValueError("Choose FakeModel or configure the DeepSeek key")
    if any(part.is_relative_to(state_dir) for p in identities for part in p.workspaces):
        raise ValueError("Web state directory cannot contain a registered workspace")
    context_window = web_model_config(provider).context_window_tokens
    tasks: set[asyncio.Task[None]] = set()
    controls: dict[tuple[str, str], Event] = {}
    repair_roots: set[Path] = set()
    submission_lock = asyncio.Lock()
    skills = SkillLibrary(skill_roots)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        with db.service_lock(engine, queued=queued):
            for token_principal in identities:
                db.register(
                    engine,
                    str(token_principal.tenant),
                    {
                        k: token_principal.workspace_label(v)
                        for k, v in token_principal.workspace_map().items()
                    },
                )
                if token_principal.member:
                    db.register_member(
                        engine, token_principal.member, list(token_principal.workspace_map())
                    )
            if legacy_owner is not None and (access := legacy_owner.access) is not None:
                db.adopt_legacy_threads(engine, str(legacy_owner.tenant), access)
            if not queued:
                for tenant in {str(principal.tenant) for principal in principals.values()}:
                    recover_interrupted(engine, tenant, include_claimed=False)
            try:
                yield
            finally:
                for stop in controls.values():
                    stop.set()
                if tasks:
                    await asyncio.gather(*tuple(tasks), return_exceptions=True)

    app = FastAPI(title="StellarWeaver local workbench", lifespan=lifespan)
    allowed_hosts = ["127.0.0.1", "localhost"]
    if oidc and (host := urlsplit(oidc.config.origin).hostname):
        allowed_hosts.append(host)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    browser_identities = {(str(p.tenant), p.member_id): p for p in (subjects or {}).values()}

    def verify_browser_write(request: Request) -> None:
        origin = request.headers.get("Origin")
        expected = oidc.config.origin if oidc else str(request.base_url).rstrip("/")
        if (
            (oidc is None and local is None)
            or request.headers.get("X-StellarWeaver-Request") != "1"
            or (origin is not None and origin != expected)
        ):
            raise HTTPException(403, "Same-origin browser request required")

    @app.exception_handler(OperationalError)
    async def database_unavailable(_: Request, __: OperationalError) -> JSONResponse:
        return JSONResponse({"detail": "Database temporarily unavailable"}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, error: RequestValidationError) -> JSONResponse:
        if request.method == "POST" and request.url.path in {
            "/api/v1/auth/register",
            "/api/v1/auth/login",
        }:
            return JSONResponse(
                {"detail": "请填写有效账号名和至少 8 个字符的密码。"}, status_code=422
            )
        return await request_validation_exception_handler(request, error)

    @app.middleware("http")
    async def prevent_browser_cache(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def principal(
        request: Request, authorization: Annotated[str | None, Header()] = None
    ) -> Principal:
        if authorization and authorization.startswith("Bearer "):
            candidate = authorization[7:]
            if candidate.isascii():
                for key, value in principals.items():
                    if secrets.compare_digest(candidate, key):
                        return value
        if (
            authorization is None
            and (oidc or local)
            and (cookie := request.cookies.get(SESSION_COOKIE))
        ):
            identity = session_member(engine, cookie)
            account = browser_identities.get(identity) if identity else None
            if account is None and identity and local:
                tenant, member = identity
                if name := local_accounts.lookup(engine, tenant, member):
                    account = Principal(
                        UUID(tenant),
                        (),
                        member=Member(
                            id=UUID(member),
                            tenant=UUID(tenant),
                            name=name,
                            role="admin",
                            workspace_indexes=(0,),
                        ),
                        local_projects=tuple(local_accounts.projects(engine, tenant, member)),
                    )
            if account is not None:
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    verify_browser_write(request)
                request.state.web_session = cookie
                return account
        raise HTTPException(401, "请先登录。")

    require_principal = Depends(principal)

    def require_write(p: Principal) -> None:
        if not p.can_write:
            raise HTTPException(403, "This account has read-only project access")

    def may_view_experiment(p: Principal) -> bool:
        return (
            experiment_file is not None
            and local is not None
            and p.local_projects is not None
            and p.tenant == local.legacy_tenant
            and p.member is not None
            and p.member.id == local.legacy_member
        )

    @app.get("/api/v1/auth/config")
    def auth_config() -> JSONDict:
        return {"oidc": oidc is not None, "bearer": bool(principals), "password": local is not None}

    def issue_cookie(request: Request, identity: tuple[str, str]) -> Response:
        if old := request.cookies.get(SESSION_COOKIE):
            logout(engine, old)
        response = Response(status_code=204)
        response.set_cookie(
            SESSION_COOKIE,
            create_session(engine, *identity),
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="lax",
        )
        return response

    @app.post("/api/v1/auth/register", status_code=204)
    def register_account(request: Request, body: Credentials) -> Response:
        if local is None:
            raise HTTPException(404)
        verify_browser_write(request)
        try:
            identity = local_accounts.enroll(engine, local, body.username, body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except local_accounts.AccountExists as exc:
            raise HTTPException(409, "账号名已被使用。") from exc
        return issue_cookie(request, identity)

    @app.post("/api/v1/auth/login", status_code=204)
    def login_account(request: Request, body: Credentials) -> Response:
        if local is None:
            raise HTTPException(404)
        verify_browser_write(request)
        try:
            identity = local_accounts.authenticate(engine, body.username, body.password)
        except ValueError:
            identity = None
        if identity is None:
            raise HTTPException(401, "账号名或密码不正确。")
        return issue_cookie(request, identity)

    @app.get("/api/v1/auth/login", response_model=None)
    async def login() -> Response:
        if oidc is None:
            raise HTTPException(404, "OIDC login unavailable")
        try:
            target, state = await oidc.begin()
        except (LoginRejected, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(503, "Identity provider unavailable") from exc
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            FLOW_COOKIE,
            state,
            max_age=FLOW_SECONDS,
            path=CALLBACK,
            httponly=True,
            secure=oidc.config.secure,
            samesite="lax",
        )
        return response

    @app.get(CALLBACK, response_model=None)
    async def login_callback(
        request: Request,
        state: Annotated[str, Query(max_length=100)] = "",
        code: Annotated[str, Query(max_length=4096)] = "",
    ) -> Response:
        if oidc is None:
            raise HTTPException(404, "OIDC login unavailable")
        try:
            subject = await oidc.finish(state, request.cookies.get(FLOW_COOKIE, ""), code)
        except (
            LoginRejected,
            httpx.HTTPError,
            jwt.PyJWTError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise HTTPException(400, "Could not verify sign-in") from exc
        account = (subjects or {}).get(subject)
        if account is None or account.member_id is None:
            raise HTTPException(403, "Account has not been granted project access")
        if old := request.cookies.get(SESSION_COOKIE):
            await asyncio.to_thread(logout, engine, old)
        cookie = await asyncio.to_thread(
            create_session, engine, str(account.tenant), account.member_id
        )
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(FLOW_COOKIE, path=CALLBACK, secure=oidc.config.secure, httponly=True)
        response.set_cookie(
            SESSION_COOKIE,
            cookie,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=oidc.config.secure,
            samesite="lax",
        )
        return response

    @app.post("/api/v1/auth/logout", status_code=204)
    async def sign_out(request: Request) -> Response:
        verify_browser_write(request)
        if cookie := request.cookies.get(SESSION_COOKIE):
            await asyncio.to_thread(logout, engine, cookie)
        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE, secure=bool(oidc and oidc.config.secure), httponly=True
        )
        return response

    @app.get("/healthz")
    def health() -> JSONDict:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            raise HTTPException(503, "Database unavailable") from exc
        return {"status": "ok"}

    @app.get("/api/v1/workspaces")
    async def workspaces(p: Principal = require_principal) -> JSONDict:
        projects: list[JSONDict] = [
            {"id": k, "label": p.workspace_label(v)} for k, v in p.workspace_map().items()
        ]
        if p.local_projects is not None and local:
            projects = [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "default_label": item["default_label"],
                    "alias": item["alias"],
                    "path": local.describe(item["relative_path"])
                    if item["relative_path"] is not None
                    else None,
                    "kind": "project" if item["relative_path"] is not None else "chat",
                }
                for item in p.local_projects
            ]
        return {
            "provider": provider,
            "context_window_tokens": context_window,
            "repair_enabled": enable_repair and p.can_write,
            "can_submit": p.can_write,
            "account": {"name": p.member.name, "role": p.member.role} if p.member else None,
            "isolated": isolated,
            "workspaces": projects,
            "manage_workspaces": p.local_projects is not None,
            "experiment_available": may_view_experiment(p),
        }

    @app.get("/api/v1/workspaces/folders")
    def browse_folders(p: Principal = require_principal, path: str = "") -> JSONDict:
        if local is None or p.local_projects is None:
            raise HTTPException(404)
        try:
            return local.folders(path)
        except (OSError, ValueError) as exc:
            raise HTTPException(404, "该项目目录不可访问。") from exc

    @app.post("/api/v1/workspaces", status_code=204)
    def create_workspace(body: ProjectFolder, p: Principal = require_principal) -> Response:
        if local is None or p.local_projects is None or p.member_id is None:
            raise HTTPException(404)
        try:
            local_accounts.add_project(engine, local, str(p.tenant), p.member_id, body.path)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "请选择本机项目目录内可访问的 Git 仓库。") from exc
        return Response(status_code=204)

    @app.patch("/api/v1/workspaces/{workspace_id}")
    def set_workspace_alias(
        workspace_id: UUID, body: WorkspaceAlias, p: Principal = require_principal
    ) -> JSONDict:
        require_write(p)
        if local is None or p.local_projects is None or p.member_id is None:
            raise HTTPException(404)
        renamed = local_accounts.rename_project(
            engine, str(p.tenant), p.member_id, str(workspace_id), body.alias or None
        )
        if renamed is None:
            raise HTTPException(404, "找不到项目工作区。")
        return renamed

    @app.get("/api/v1/threads")
    async def threads(p: Principal = require_principal) -> list[JSONDict]:
        return await asyncio.to_thread(db.recent_threads, engine, str(p.tenant), p.access)

    @app.patch("/api/v1/threads/{session_id}", status_code=204)
    def set_thread_archive(
        session_id: UUID, body: ArchiveThread, p: Principal = require_principal
    ) -> Response:
        require_write(p)
        if p.access is None:
            raise HTTPException(404)
        try:
            changed = db.archive_thread(
                engine, str(p.tenant), str(session_id), p.access, body.archived
            )
        except db.RunUnavailable as exc:
            raise HTTPException(409, "任务完成后可归档。") from exc
        if not changed:
            raise HTTPException(404, "找不到会话。")
        return Response(status_code=204)

    @app.get("/api/v1/runs")
    async def runs(
        p: Principal = require_principal,
        session_id: UUID | None = None,
    ) -> list[JSONDict]:
        return await asyncio.to_thread(
            db.recent,
            engine,
            str(p.tenant),
            str(session_id) if session_id else None,
            p.access,
        )

    def resolve_workspace(p: Principal, workspace_id: str) -> Path | None:
        """Resolve an authorized project. Only ordinary chat has no project root."""
        if p.local_projects is not None and local:
            project = next((item for item in p.local_projects if item["id"] == workspace_id), None)
            if project is None:
                raise HTTPException(404, "找不到工作区。")
            if project["relative_path"] is None:
                return None
            try:
                return local.folder(project["relative_path"])
            except (OSError, ValueError) as exc:
                raise HTTPException(404, "该项目目录不可访问。") from exc
        root = p.workspace_map().get(workspace_id)
        if root is None or not root.is_dir() or root.resolve() != root:
            raise HTTPException(404, "Workspace not available")
        return root

    @app.post("/api/v1/runs", status_code=202)
    async def start(body: Submit, p: Principal = require_principal) -> JSONDict:
        return await submit_run(body, p)

    def workspace_skill_library(p: Principal, workspace_id: str, root: Path | None) -> SkillLibrary:
        if root is not None and db.project_skill_trust(
            engine, str(p.tenant), p.member_id, workspace_id
        ):
            return SkillLibrary((*skills.roots, ("项目", project_skill_root(root))))
        return skills

    async def submit_run(
        body: Submit,
        p: Principal,
        *,
        expected_previous_run: str | None = None,
        undo: JSONDict | None = None,
    ) -> JSONDict:
        require_write(p)
        workspace_id = str(body.workspace_id)
        root = resolve_workspace(p, workspace_id)
        chat = root is None
        if chat and body.mode != "read-only":
            raise HTTPException(422, "普通对话不使用项目工具。")
        prompt = body.prompt.strip()
        if not prompt:
            raise HTTPException(422, "Prompt is required")
        tenant = str(p.tenant)
        repair = body.mode in db.REPAIR_MODES
        if repair and not enable_repair:
            raise HTTPException(403, "Local repair is disabled")
        selected = None
        library = skills
        if not queued or body.skills:
            try:
                library = await asyncio.to_thread(workspace_skill_library, p, workspace_id, root)
            except ToolError as exc:
                raise HTTPException(409, str(exc)) from exc
        if body.skills is not None:
            refs = [item.model_dump() for item in body.skills]
            if len({item["id"] for item in refs}) != len(refs):
                raise HTTPException(422, "同一个技能只能选择一次。")
            try:
                loaded = await asyncio.to_thread(library.pinned, refs)
            except ToolError as exc:
                raise HTTPException(409, str(exc)) from exc
            selected = [
                {**ref, "name": skill["name"], "source": skill["source"]}
                for ref, skill in zip(refs, loaded, strict=True)
            ]
        async with submission_lock:
            busy = repair and root in repair_roots
            try:
                ids, created = await asyncio.to_thread(
                    db.submit,
                    engine,
                    tenant,
                    workspace_id,
                    str(body.request_id),
                    prompt,
                    provider,
                    body.mode,
                    allow_create=queued or (not busy and len(tasks) < 4),
                    enqueue=queued,
                    session_id=str(body.session_id) if body.session_id else None,
                    continuation=body.continuation if body.session_id else None,
                    expected_previous_run=expected_previous_run,
                    acknowledge_interrupted=body.acknowledge_interrupted,
                    isolated=isolated and not chat,
                    member=p.member_id,
                    skills=selected,
                    undo=undo,
                    context_window_tokens=context_window if undo is None else None,
                )
            except db.RequestConflict as exc:
                raise HTTPException(409, "Request ID already used with different input") from exc
            except db.ExecutionModeConflict as exc:
                raise HTTPException(503, "Workbench database is in inline mode") from exc
            except db.ThreadUnavailable as exc:
                raise HTTPException(
                    409,
                    "请从原线程最新完成的改动发起反馈。"
                    if expected_previous_run
                    else "Thread unavailable or already running",
                ) from exc
            except db.InterruptedConfirmationRequired as exc:
                raise HTTPException(
                    409, "Review the interrupted run and confirm continuation"
                ) from exc
            except db.RunUnavailable as exc:
                raise HTTPException(
                    409 if busy else 429,
                    "Repair already running in this workspace"
                    if busy
                    else "Workbench queue is full"
                    if queued
                    else "Local run limit reached",
                ) from exc
            except db.QuotaReached as exc:
                raise HTTPException(429, "Account quota reached") from exc
            if created and not queued:
                # Local accounts, including ordinary chat, always use a queue worker.
                assert root is not None
                journal = PgJournal(
                    engine,
                    tenant,
                    ids["session_id"],
                    ids["run_id"],
                    root,
                    state_dir,
                    repair=repair,
                )
                key = (tenant, ids["run_id"])
                stop = controls[key] = Event()
                if repair:
                    repair_roots.add(root)
                task = asyncio.create_task(
                    asyncio.to_thread(
                        execute,
                        journal,
                        prompt,
                        stop,
                        provider,
                        api_key,
                        approval_ttl,
                        None,
                        body.continuation if body.session_id else None,
                        auto=body.mode == "auto",
                        skill_roots=library.roots,
                        selected_skills=selected,
                        context_window_tokens=context_window,
                    )
                )
                tasks.add(task)

                def settled(finished: asyncio.Task[None]) -> None:
                    tasks.discard(finished)
                    controls.pop(key, None)
                    if repair:
                        repair_roots.discard(root)
                    if not finished.cancelled() and finished.exception() is not None:
                        # Keep the DB status recoverable and don't log driver connection details.
                        logging.getLogger(__name__).error("Run journal unavailable")

                task.add_done_callback(settled)
        return ids

    async def lookup(p: Principal, run_id: UUID) -> JSONDict:
        row = await asyncio.to_thread(db.snapshot, engine, str(p.tenant), str(run_id), p.access)
        if row is None:
            raise HTTPException(404, "Run not found")
        row["can_continue"] = p.can_write and (p.member is None or row["member_id"] == p.member_id)
        row.pop("member_id", None)
        if row["outcome"]:
            row["outcome"] = public_event({"type": "run.end", "payload": row["outcome"]})["payload"]
        return row

    @app.get("/api/v1/runs/{run_id}")
    async def inspect(run_id: UUID, p: Principal = require_principal) -> JSONDict:
        tenant = str(p.tenant)
        row = await lookup(p, run_id)
        if row["status"] == "active" and row["mode"] in db.REPAIR_MODES and p.can_write:

            def read_approval() -> JSONDict | None:
                with db.scoped(engine, tenant) as conn:
                    return pending(conn, str(run_id))

            row["pending_approval"] = await asyncio.to_thread(read_approval)
        return row

    async def mutation_journal(p: Principal, run_id: UUID) -> PgJournal:
        require_write(p)
        row = await lookup(p, run_id)
        return PgJournal(
            engine,
            str(p.tenant),
            row["session_id"],
            str(run_id),
            Path("."),
            state_dir,
            member_id=p.member_id,
        )

    @app.post("/api/v1/runs/{run_id}/approvals/{approval_id}", status_code=204)
    async def approve(
        run_id: UUID,
        approval_id: UUID,
        body: Decision,
        p: Principal = require_principal,
    ) -> Response:
        journal = await mutation_journal(p, run_id)
        try:
            await asyncio.to_thread(decide, journal, str(approval_id), body.decision)
        except DecisionConflict as exc:
            raise HTTPException(409, "Approval is no longer pending") from exc
        return Response(status_code=204)

    @app.post("/api/v1/runs/{run_id}/cancel", status_code=204)
    async def cancel(
        run_id: UUID,
        p: Principal = require_principal,
    ) -> Response:
        journal = await mutation_journal(p, run_id)
        try:
            await asyncio.to_thread(request_cancel, journal)
        except DecisionConflict as exc:
            raise HTTPException(409, "Run already ended") from exc
        stop = controls.get((journal.tenant, journal.run_id))
        if stop is not None:
            stop.set()
        return Response(status_code=204)

    @app.get("/api/v1/runs/{run_id}/report")
    async def report(
        run_id: UUID,
        p: Principal = require_principal,
    ) -> JSONDict:
        tenant = str(p.tenant)
        await lookup(p, run_id)
        return await get_report(tenant, run_id, actor=p.member_id)

    @app.get("/api/v1/runs/{run_id}/export")
    async def export(
        run_id: UUID, format: Literal["json", "html"] = "json", p: Principal = require_principal
    ) -> Response:
        row = await lookup(p, run_id)
        if row["status"] == "active":
            raise HTTPException(409, "请在本轮运行结束后导出。")

        def read_export() -> str:
            with db.scoped(engine, str(p.tenant)) as conn:
                output = render_export(build_export(conn, row), format)
                db.audit(conn, str(p.tenant), p.member_id, str(run_id), "run.export")
                return output

        try:
            output = await asyncio.to_thread(read_export)
        except ExportLimitExceeded as exc:
            raise HTTPException(413, "本轮记录超过导出大小上限。") from exc
        return Response(
            output,
            media_type="application/json" if format == "json" else "text/html",
            headers={
                "Content-Disposition": f'attachment; filename="stellarweaver-{run_id}.{format}"'
            },
        )

    def can_undo(row: JSONDict) -> bool:
        return bool(
            queued
            and enable_repair
            and row["isolated"]
            and row["can_continue"]
            and not row["is_review"]
            and row["status"] != "active"
            and row["status"] != "interrupted"
            and not (row.get("outcome") or {}).get("unfinished_tools")
        )

    @app.get("/api/v1/runs/{run_id}/undo")
    async def undo_preview(run_id: UUID, p: Principal = require_principal) -> JSONDict:
        row = await lookup(p, run_id)
        if not can_undo(row):
            return {"target": None}

        def find_target() -> JSONDict:
            with db.scoped(engine, str(p.tenant)) as conn:
                latest = conn.execute(
                    text("""
                    SELECT r.id::text, s.archived_at FROM sw.runs r JOIN sw.sessions s
                      ON (s.tenant_id,s.id)=(r.tenant_id,r.session_id)
                    WHERE s.id=:session ORDER BY r.created_at DESC,r.id DESC LIMIT 1
                    """),
                    {"session": row["session_id"]},
                ).one_or_none()
                edit = (
                    latest_edit(conn, row["session_id"])
                    if latest and latest[0] == str(run_id) and latest[1] is None
                    else None
                )
                return {
                    "target": {
                        "operation_id": edit["operation_id"],
                        "tool_name": edit["tool_name"],
                        "path": edit["data"]["path"],
                    }
                    if edit
                    else None
                }

        return await asyncio.to_thread(find_target)

    @app.post("/api/v1/runs/{run_id}/undo", status_code=202)
    async def undo_file(
        run_id: UUID, body: UndoRequest, p: Principal = require_principal
    ) -> JSONDict:
        require_write(p)
        row = await lookup(p, run_id)
        if not can_undo(row):
            raise HTTPException(409, "请从当前项目线程发起撤销。")
        return await submit_run(
            Submit(
                workspace_id=UUID(row["workspace_id"]),
                request_id=body.request_id,
                session_id=UUID(row["session_id"]),
                prompt="撤销最近一次文件修改",
                mode="repair",
                continuation="history",
            ),
            p,
            expected_previous_run=str(run_id),
            undo={"operation_id": str(body.operation_id)},
        )

    async def get_report(tenant: str, run_id: UUID, *, actor: str | None = None) -> JSONDict:
        def read_report() -> JSONDict | None:
            with db.scoped(engine, tenant) as conn:
                value: JSONDict | None = conn.execute(
                    text("SELECT report FROM sw.runs WHERE id=:id"), {"id": str(run_id)}
                ).scalar_one_or_none()
                if value is not None:
                    db.audit(conn, tenant, actor, str(run_id), "report.read")
                return value

        result = await asyncio.to_thread(read_report)
        if result is None:
            raise HTTPException(404, "Delivery report unavailable")
        return result

    @app.get("/api/v1/runs/{run_id}/patch")
    async def download_patch(run_id: UUID, p: Principal = require_principal) -> Response:
        await lookup(p, run_id)
        candidate = (await get_report(str(p.tenant), run_id, actor=p.member_id)).get("review")
        if not candidate:
            raise HTTPException(404, "Patch unavailable")
        hunks = [hunk["id"] for file in candidate["files"] for hunk in file["hunks"]]
        if not hunks:
            raise HTTPException(404, "Patch unavailable")
        return Response(
            selected_patch(candidate, hunks),
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="stellarweaver-{run_id}.patch"'},
        )

    async def review_source(p: Principal, run_id: UUID) -> tuple[JSONDict, Path, JSONDict]:
        require_write(p)
        if not isolated or not enable_repair:
            raise HTTPException(403, "Isolated review is disabled")
        row = await lookup(p, run_id)
        if (
            not row["isolated"]
            or row["status"] != "completed"
            or row["mode"] not in db.REPAIR_MODES
        ):
            raise HTTPException(409, "Completed isolated repair required")
        root = resolve_workspace(p, row["workspace_id"])
        if root is None:
            raise HTTPException(409, "Completed isolated repair required")
        delivery = await get_report(str(p.tenant), run_id)
        if not delivery.get("review"):
            raise HTTPException(409, "No reviewable patch")
        return row, root, delivery

    @app.post("/api/v1/runs/{run_id}/review/feedback", status_code=202)
    async def send_review_feedback(
        run_id: UUID, body: ReviewFeedback, p: Principal = require_principal
    ) -> JSONDict:
        row, _, delivery = await review_source(p, run_id)
        if not row["can_continue"] or row["is_review"]:
            raise HTTPException(409, "请从可续接的项目线程发起反馈。")
        try:
            prompt = feedback_prompt(
                delivery["review"], str(run_id), body.path, body.hunk, body.text
            )
        except ToolError as exc:
            raise HTTPException(422, str(exc)) from exc
        return await submit_run(
            Submit(
                workspace_id=UUID(row["workspace_id"]),
                request_id=body.request_id,
                session_id=UUID(row["session_id"]),
                prompt=prompt,
                mode=row["mode"],
            ),
            p,
            expected_previous_run=str(run_id),
        )

    preflight_slot = asyncio.Lock()

    @app.post("/api/v1/runs/{run_id}/review/preflight")
    async def preflight_review(
        run_id: UUID, body: PatchSelection, p: Principal = require_principal
    ) -> JSONDict:
        row, root, delivery = await review_source(p, run_id)
        if preflight_slot.locked():
            raise HTTPException(429, "已有补丁预检正在进行，请稍后再试。")
        try:
            async with preflight_slot:
                result = await asyncio.to_thread(
                    preflight_patch,
                    root,
                    str(p.tenant),
                    str(run_id),
                    delivery["review"],
                    body.hunks,
                )
        except (OSError, ValueError, ToolError) as exc:
            if isinstance(exc, ToolError):
                raise HTTPException(422, exc.message) from exc
            raise HTTPException(422, "无法读取目标工作区进行预检。") from exc
        entire = result.pop("entire_selection")
        result["verified_selection"] = bool(
            row["is_review"] and delivery.get("verification") == "passed" and entire
        )
        result["handoff_ready"] = result["verified_selection"] and result["status"] == "ready"
        return result

    @app.post("/api/v1/runs/{run_id}/review", status_code=202)
    async def review(
        run_id: UUID,
        body: ReviewSelection,
        p: Principal = require_principal,
    ) -> JSONDict:
        row, _, delivery = await review_source(p, run_id)
        tenant = str(p.tenant)
        workspace = row["workspace_id"]
        candidate = delivery["review"]
        try:
            patch = selected_patch(candidate, body.hunks)
            ids, _ = await asyncio.to_thread(
                db.submit,
                engine,
                tenant,
                workspace,
                str(body.request_id),
                "Apply selected hunks and run configured verification",
                provider,
                "repair",
                enqueue=True,
                isolated=True,
                member=p.member_id,
                review={
                    "run_id": str(run_id),
                    "session_id": row["session_id"],
                    "base": candidate["base"],
                    "patch": patch.decode(),
                },
            )
        except ToolError as exc:
            raise HTTPException(422, str(exc)) from exc
        except db.RequestConflict as exc:
            raise HTTPException(409, "Request ID already used with different input") from exc
        except db.ExecutionModeConflict as exc:
            raise HTTPException(503, "Workbench database is in inline mode") from exc
        except db.RunUnavailable as exc:
            raise HTTPException(429, "Workbench queue is full") from exc
        except db.QuotaReached as exc:
            raise HTTPException(429, "Account quota reached") from exc
        except db.ThreadUnavailable as exc:
            raise HTTPException(404, "Workspace not available") from exc
        return ids

    @app.get("/api/v1/runs/{run_id}/events", response_class=StreamingResponse)
    async def stream(
        run_id: UUID,
        request: Request,
        after: Annotated[int, Query(ge=0)] = 0,
        p: Principal = require_principal,
    ) -> StreamingResponse:
        tenant = str(p.tenant)
        await lookup(p, run_id)
        previous = request.headers.get("last-event-id", "")
        if previous:
            if not previous.isascii() or not previous.isdecimal() or len(previous) > 18:
                raise HTTPException(400, "Invalid event cursor")
            after = max(after, int(previous))
        if not 0 <= after < 2**63 - 1:
            raise HTTPException(400, "Invalid event cursor")

        async def events() -> AsyncIterator[str]:
            cursor = after
            cookie = getattr(request.state, "web_session", None)
            next_session_check = 0.0
            while not await request.is_disconnected():
                # Read status before the page. If terminal, its event already committed.
                try:
                    if cookie and time.monotonic() >= next_session_check:
                        if await asyncio.to_thread(session_member, engine, cookie) != (
                            tenant,
                            p.member_id,
                        ):
                            return
                        next_session_check = time.monotonic() + 2
                    row = await lookup(p, run_id)
                    page = await asyncio.to_thread(
                        db.event_page, engine, tenant, str(run_id), cursor
                    )
                except OperationalError:
                    # The browser reconnects with its last committed cursor.
                    return
                for event in page:
                    cursor = event["seq"]
                    if not p.can_write and event["type"] == "approval.requested":
                        continue
                    yield f"id: {cursor}\ndata: {json.dumps(public_event(event), ensure_ascii=False)}\n\n"
                if len(page) < 128:
                    if row["status"] != "active":
                        return
                    yield ": keep-alive\n\n"
                    await asyncio.sleep(0.15)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/admin/audit")
    async def read_audit(p: Principal = require_principal) -> list[JSONDict]:
        if p.member is None or p.member.role != "admin" or p.access is None:
            raise HTTPException(403, "Administrator access required")
        return await asyncio.to_thread(db.audit_rows, engine, str(p.tenant), p.access)

    @app.get("/api/v1/admin/usage")
    async def read_usage(p: Principal = require_principal) -> list[JSONDict]:
        if p.member is None or p.member.role != "admin" or p.access is None:
            raise HTTPException(403, "Administrator access required")
        return await asyncio.to_thread(db.usage_rows, engine, str(p.tenant), p.access)

    @app.get("/api/v1/admin/usage/overview")
    async def read_usage_today(p: Principal = require_principal) -> JSONDict:
        if p.member is None or p.member.role != "admin" or p.access is None:
            raise HTTPException(403, "Administrator access required")
        return await asyncio.to_thread(db.usage_today, engine, str(p.tenant), p.access)

    def published_experiments(p: Principal) -> PublishedExperiments:
        if not may_view_experiment(p) or experiment_file is None:
            raise HTTPException(404)
        return PublishedExperiments(experiment_file)

    @app.get("/api/v1/experiment")
    def local_experiment(p: Principal = require_principal) -> JSONDict:
        library = published_experiments(p)
        try:
            return read_legacy(library.legacy)
        except FileNotFoundError as exc:
            raise HTTPException(404, "实验记录尚未发布。") from exc
        except ExperimentTooLarge as exc:
            raise HTTPException(413, "实验记录超出 8 MiB。") from exc
        except OSError as exc:
            raise HTTPException(503, "无法读取实验记录。") from exc
        except (ValueError, RecursionError) as exc:
            raise HTTPException(503, "实验记录格式不正确。") from exc

    @app.get("/api/v1/experiments")
    def experiment_catalog(p: Principal = require_principal) -> JSONDict:
        return published_experiments(p).list()

    @app.get("/api/v1/experiments/compare")
    def compare_experiments(
        left: str,
        right: str,
        vary: str = "",
        p: Principal = require_principal,
    ) -> JSONDict:
        library = published_experiments(p)
        try:
            return cast(
                JSONDict,
                redact(library.comparison(left, right, frozenset(filter(None, vary.split(","))))),
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "实验记录尚未发布。") from exc
        except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise HTTPException(422, "记录或对照变量无法比较。") from exc

    @app.get("/api/v1/experiments/{name}")
    def experiment_detail(name: str, p: Principal = require_principal) -> JSONDict:
        library = published_experiments(p)
        try:
            return library.detail(name)
        except FileNotFoundError as exc:
            raise HTTPException(404, "实验记录尚未发布。") from exc
        except ExperimentTooLarge as exc:
            raise HTTPException(413, "实验记录超出 8 MiB。") from exc
        except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise HTTPException(503, "实验记录格式不正确。") from exc

    @app.get("/api/v1/skills")
    def list_skills(
        p: Principal = require_principal, refresh: bool = False, workspace_id: UUID | None = None
    ) -> JSONDict:
        if workspace_id is None:
            return skills.list(refresh=refresh)
        key = str(workspace_id)
        root = resolve_workspace(p, key)
        trusted = db.project_skill_trust(engine, str(p.tenant), p.member_id, key) if root else None
        try:
            library = (
                SkillLibrary((*skills.roots, ("项目", project_skill_root(root))))
                if trusted and root is not None
                else skills
            )
        except ToolError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            **library.list(refresh=refresh),
            "project_trusted": trusted,
            "can_manage_project": trusted is not None and p.can_write,
        }

    @app.put("/api/v1/workspaces/{workspace_id}/skill-trust")
    def set_skill_trust(
        workspace_id: UUID, body: SkillTrust, p: Principal = require_principal
    ) -> JSONDict:
        require_write(p)
        key = str(workspace_id)
        root = resolve_workspace(p, key)
        if root is None or p.member_id is None:
            raise HTTPException(404, "找不到项目工作区。")
        try:
            if body.enabled:
                project_skill_root(root)
        except ToolError as exc:
            raise HTTPException(409, str(exc)) from exc
        enabled = db.project_skill_trust(engine, str(p.tenant), p.member_id, key, body.enabled)
        if enabled is None:
            raise HTTPException(404, "找不到项目工作区。")
        return {"project_trusted": enabled}

    @app.get("/api/v1/skills/{skill_id}")
    def read_skill(
        skill_id: str, p: Principal = require_principal, workspace_id: UUID | None = None
    ) -> JSONDict:
        try:
            if workspace_id is None:
                return skills.detail(skill_id)
            key = str(workspace_id)
            return workspace_skill_library(p, key, resolve_workspace(p, key)).detail(skill_id)
        except ToolError as exc:
            raise HTTPException(404 if exc.code == "SKILL_NOT_FOUND" else 409, exc.message) from exc

    if static_dir is not None:
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="workbench")
    return app
