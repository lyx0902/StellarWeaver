"""G5 authorization, per-member accounting, and signed OIDC acceptance."""

import asyncio
import base64
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
pytest.importorskip("jwt")

import httpx  # noqa: E402
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from stellarweaver.domain import Message, ToolCall  # noqa: E402
from stellarweaver.runtime.loop import BudgetExceeded  # noqa: E402
from stellarweaver.tools.development import decode_development  # noqa: E402
from stellarweaver.web import db  # noqa: E402
from stellarweaver.web.app import Principal, create_app  # noqa: E402
from stellarweaver.web.approval import ApprovalGate, DecisionConflict, decide, pending  # noqa: E402
from stellarweaver.web.broker import create_broker  # noqa: E402
from stellarweaver.web.identity import Member  # noqa: E402
from stellarweaver.web.isolation import DockerRunner, Isolation  # noqa: E402
from stellarweaver.web.journal import PgJournal  # noqa: E402
from stellarweaver.web.oidc import CALLBACK, OIDC, SESSION_COOKIE, OIDCConfig  # noqa: E402


def test_executor_passes_validated_archive_to_child_from_start(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Isolation, "reap", lambda _: None)

    async def verify(_, archive, *_args, **_kwargs):
        raw = os.read(archive.fileno(), 10240)
        with tarfile.open(fileobj=io.BytesIO(raw)) as snapshot:
            assert snapshot.getnames() == ["test_small.py"]
        return {"status": "ok", "data": {"process_management": {"cleanup_confirmed": True}}}

    monkeypatch.setattr(DockerRunner, "run_archive", verify)
    package = io.BytesIO()
    with tarfile.open(mode="w", fileobj=package) as snapshot:
        entry = tarfile.TarInfo("test_small.py")
        content = b"pass\n"
        entry.size = len(content)
        snapshot.addfile(entry, io.BytesIO(content))
    token = uuid4().hex
    broker = create_broker(
        Isolation("fixture:local", (("python", "-m", "unittest"),), "fixture"), token
    )
    with TestClient(broker) as client:
        response = client.post(
            f"/verify/{uuid4()}?command=0&timeout=5",
            content=package.getvalue(),
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_executor_accepts_structured_action_without_raw_argv(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Isolation, "reap", lambda _: None)
    actions = []

    async def verify(_self, archive, argv, **_kwargs):
        operation = decode_development(argv)
        assert operation is not None
        action, arguments = operation
        assert archive.tell() == 0
        actions.append((action, arguments.target, arguments.framework))
        return {"status": "ok", "data": {"process_management": {"cleanup_confirmed": True}}}

    monkeypatch.setattr(DockerRunner, "run_archive", verify)
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as content:
        entry = tarfile.TarInfo("test_fixture.py")
        entry.size = 5
        content.addfile(entry, io.BytesIO(b"pass\n"))
    token = uuid4().hex
    app = create_broker(
        Isolation("sha256:" + "a" * 64, (("python", "-V"),), "fixture", development=True),
        token,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:

        def request(**params):
            return client.post(
                f"/verify/{uuid4()}",
                params={"timeout": 5, **params},
                content=archive.getvalue(),
                headers={"Authorization": "Bearer " + token},
            )

        assert (
            request(
                action="discover_tests", target="test_fixture.py", framework="unittest"
            ).status_code
            == 200
        )
        assert (
            request(action="run_tests", target=".", framework="unittest", command=0).status_code
            == 422
        )
        assert (
            request(action="check_syntax", target="../outside.py", framework="unittest").status_code
            == 422
        )
    assert actions == [("discover_tests", "test_fixture.py", "unittest")]


@pytest.fixture
def engine():
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("requires the dedicated PostgreSQL acceptance database")
    engine = db.engine_for(dsn)
    yield engine
    engine.dispose()


def account(tenant: UUID, path: Path, role: str, name: str, **extra) -> Principal:
    member = Member.model_validate(
        {
            "id": uuid4(),
            "tenant": tenant,
            "name": name,
            "role": role,
            "workspace_indexes": [0],
            **extra,
        }
    )
    return Principal(tenant, (path.resolve(),), member=member)


@pytest.mark.postgres
@pytest.mark.parametrize("scenario", ["replay", "expired", "fingerprint_changed"])
async def test_one_shot_search_approval_is_bound_to_run_tenant_and_intent(
    engine, tmp_path: Path, scenario: str
) -> None:
    tenant = str(uuid4())
    workspace = str(uuid4())

    def start_run(tenant_id: str) -> PgJournal:
        db.register(engine, tenant_id, {workspace: "Fixture"})
        ids, _ = db.submit(engine, tenant_id, workspace, str(uuid4()), "Fixture", "fake")
        return PgJournal(engine, tenant_id, ids["session_id"], ids["run_id"], tmp_path, tmp_path)

    journal = start_run(tenant)
    other_run = start_run(tenant)
    other_tenant = PgJournal(
        engine, str(uuid4()), journal.session_id, journal.run_id, tmp_path, tmp_path
    )
    query = "Python numeric types documentation"
    message = Message(
        "assistant", "", tool_calls=(ToolCall("search", "ds_search", json.dumps({"query": query})),)
    )
    assistant = journal.record_assistant(message)
    operation = journal.operation_id_for(assistant, "search")
    invocation = {
        "tool": "ds_search",
        "operation_id": operation,
        "query": query,
        "policy_binding": "fixture-policy",
        "security": {"level": "L0", "capabilities": ["network:public-search-query"]},
    }
    gate = ApprovalGate(journal, Event(), 20.0)
    async with asyncio.TaskGroup() as group:
        approval = group.create_task(gate.approve(invocation))
        async with asyncio.timeout(3):
            while True:
                with db.scoped(engine, tenant) as conn:
                    preview = pending(conn, journal.run_id)
                if preview is not None:
                    break
                await asyncio.sleep(0.02)
        assert preview["query"] == query
        assert preview["security"]["level"] == "L0"
        for unrelated in (other_run, other_tenant):
            with pytest.raises(DecisionConflict):
                decide(unrelated, operation, "approve")
        if scenario == "expired":
            with db.scoped(engine, tenant) as conn:
                conn.execute(
                    text("UPDATE sw.approvals SET expires_at=clock_timestamp() WHERE id=:id"),
                    {"id": operation},
                )
            with pytest.raises(DecisionConflict):
                decide(journal, operation, "approve")
        elif scenario == "fingerprint_changed":
            with db.scoped(engine, tenant) as conn:
                conn.execute(
                    text("UPDATE sw.approvals SET fingerprint='different' WHERE id=:id"),
                    {"id": operation},
                )
            decide(journal, operation, "approve")
        else:
            decide(journal, operation, "approve")
        assert await approval == ("once" if scenario == "replay" else "deny")
        with db.scoped(engine, tenant) as conn:
            state = conn.execute(
                text("SELECT state FROM sw.approvals WHERE id=:id"), {"id": operation}
            ).scalar_one()
        assert (
            state
            == {"replay": "consumed", "expired": "expired", "fingerprint_changed": "cancelled"}[
                scenario
            ]
        )
        event_type = {
            "replay": "approval.consumed",
            "expired": "approval.expired",
            "fingerprint_changed": "approval.invalidated",
        }[scenario]
        assert any(event.type == event_type for event in journal.load_events())
        with pytest.raises(DecisionConflict):
            decide(journal, operation, "approve")
        assert await gate.approve(invocation) == "deny"


@pytest.mark.postgres
def test_usage_days_and_read_only_skill_library(engine, tmp_path: Path) -> None:
    tenant = uuid4()
    admin = account(tenant, tmp_path, "admin", "Admin")
    viewer = account(tenant, tmp_path, "viewer", "Viewer")
    admin_key, viewer_key = uuid4().hex, uuid4().hex
    headers = {"Authorization": "Bearer " + admin_key}
    viewer_headers = {"Authorization": "Bearer " + viewer_key}
    root = tmp_path / "host-skills"
    document = root / "example" / "SKILL.md"
    document.parent.mkdir(parents=True)
    document.write_text(
        "---\nname: example\ndescription: Local fixture\n---\nOnly show this text.\n"
    )
    app = create_app(
        engine,
        {admin_key: admin, viewer_key: viewer},
        tmp_path / "state",
        queued=True,
        isolated=True,
        skill_roots=(("Fixture", root),),
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        route = "/api/v1/admin/usage/overview"
        assert client.get(route, headers=viewer_headers).status_code == 403
        empty = client.get(route, headers=headers)
        assert empty.status_code == 200, empty.text
        assert empty.json()["today"]["tokens"] == 0
        assert empty.json()["read_at"] and empty.json()["quota_resets_at"]
        assert empty.json()["quota"]["limit_tokens"] == 100_000_000
        ids, _ = db.submit(
            engine,
            str(tenant),
            next(iter(admin.workspace_map())),
            str(uuid4()),
            "Boundary fixture",
            "deepseek",
            member=admin.member_id,
        )
        with db.scoped(engine, str(tenant)) as conn:
            conn.execute(
                text("""
                INSERT INTO sw.model_usage(tenant_id,run_id,request_index,member_id,usage_day,
                                           started_at,tokens,settled,estimated) VALUES
                (:tenant,:run,1,:member,'2026-11-18','2026-11-18 15:59:00+00',17,true,false),
                (:tenant,:run,2,:member,'2026-11-18','2026-11-18 16:00:00+00',23,false,true),
                (:tenant,:run,3,:member,'2026-11-18',NULL,5,true,false),
                (:tenant,:run,4,:member,'2026-11-16',NULL,7,true,false),
                (:tenant,:run,5,:member,'2026-11-15',NULL,100,true,false)
                """),
                {"tenant": str(tenant), "run": ids["run_id"], "member": admin.member_id},
            )
        assert admin.access is not None
        previous = db.usage_today(engine, str(tenant), admin.access, at="2026-11-18T15:59:59Z")
        assert previous["day"].isoformat() == "2026-11-18" and previous["today"]["tokens"] == 17
        current = db.usage_today(engine, str(tenant), admin.access, at="2026-11-18T16:00:00Z")
        assert current["day"].isoformat() == "2026-11-19"
        assert current["today"] == dict(
            tokens=23, requests=1, estimated_requests=1, unsettled_requests=1
        )
        assert current["history"][0]["tokens"] == 45 and current["quota"]["used_tokens"] == 52
        assert current["quota_week_start"].isoformat() == "2026-11-16"
        midnight = db.usage_today(engine, str(tenant), admin.access, at="2026-11-19T00:00:00Z")
        assert midnight["today"]["tokens"] == 23 and midnight["quota"]["used_tokens"] == 52
        reset = db.usage_today(engine, str(tenant), admin.access, at="2026-11-23T00:00:00Z")
        assert reset["quota"]["used_tokens"] == 0

        route = "/api/v1/skills"
        assert client.get(route).status_code == 401
        listing = client.get(route, headers=viewer_headers).json()
        assert listing["sources"] == ["Fixture"] and not listing["errors"]
        (entry,) = listing["skills"]
        assert entry["path"] == "example/SKILL.md" and "content" not in entry
        detail = client.get(f"{route}/{entry['id']}", headers=viewer_headers).json()
        assert detail["content"] == "Only show this text."
        assert str(root) not in json.dumps(detail)
        assert client.get(f"{route}/{'0' * 64}", headers=headers).status_code == 404
        document.unlink()
        assert client.get(f"{route}/{entry['id']}", headers=headers).status_code == 409
        assert client.get(route + "?refresh=true", headers=headers).json()["skills"] == []


@pytest.mark.postgres
def test_team_access_quotas_audit_and_report_exports(engine, tmp_path: Path):
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    tenant = uuid4()
    accounts = {
        "owner": account(tenant, repo, "member", "Owner", active_limit=1, weekly_tokens=65),
        "teammate": account(tenant, repo, "member", "Teammate"),
        "outsider": account(uuid4(), repo, "member", "Outsider"),
        "other_project": account(tenant, other, "admin", "Other project"),
        "admin": account(tenant, repo, "admin", "Operator"),
        "viewer": account(tenant, repo, "viewer", "Viewer"),
    }
    tokens = {key: uuid4().hex for key in accounts}
    app = create_app(
        engine,
        {tokens[key]: v for key, v in accounts.items()},
        tmp_path / "state",
        queued=True,
        isolated=True,
        enable_repair=True,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:

        def headers(actor: str) -> dict:
            return {"Authorization": "Bearer " + tokens[actor]}

        def run_body(actor: str, **extra) -> dict:
            return {
                "workspace_id": next(iter(accounts[actor].workspace_map())),
                "request_id": str(uuid4()),
                "prompt": "Test a repair",
                "mode": "repair",
                **extra,
            }

        def get(actor: str, resource: str):
            return client.get("/api/v1/" + resource, headers=headers(actor))

        def post(actor: str, resource: str, payload: dict):
            return client.post("/api/v1/" + resource, headers=headers(actor), json=payload)

        request = run_body("owner")
        resp = post("owner", "runs", request)
        assert resp.status_code == 202, resp.text
        ids = resp.json()
        assert post("owner", "runs", request).json() == ids
        assert post("owner", "runs", run_body("owner")).status_code == 429
        assert post("viewer", "runs", run_body("viewer")).status_code == 403
        route = "runs/" + ids["run_id"]
        for actor in ("outsider", "teammate", "other_project"):
            assert get(actor, "threads").json() == []
            assert get(actor, "runs?session_id=" + ids["session_id"]).json() == []
            for suffix in ("", "/events", "/report", "/patch", "/export", "/undo"):
                assert get(actor, route + suffix).status_code == 404
            assert post(actor, route + "/cancel", {}).status_code == 404
            assert (
                post(
                    actor, route + "/approvals/" + str(uuid4()), {"decision": "approve"}
                ).status_code
                == 404
            )
        assert get("viewer", route).status_code == 200
        assert post("viewer", route + "/cancel", {}).status_code == 403
        assert get("viewer", "admin/audit").status_code == 403
        assert (
            post(
                "teammate",
                "runs",
                run_body(
                    "teammate",
                    request_id=request["request_id"],
                ),
            ).status_code
            == 202
        )

        journal = PgJournal(
            engine,
            str(tenant),
            ids["session_id"],
            ids["run_id"],
            repo,
            tmp_path / "state",
            member_id=accounts["owner"].member_id,
            metered=True,
        )
        journal.append_event(
            "model.started",
            {
                "request_index": 1,
                "input_tokens": 35,
                "max_output_tokens": 20,
            },
        )
        journal.append_event(
            "model.completed",
            {
                "request_index": 1,
                "input_tokens": 39,
                "output_tokens": 5,
                "estimated": False,
            },
        )
        with pytest.raises(BudgetExceeded, match="WEEKLY_MODEL_QUOTA"):
            journal.append_event(
                "model.started",
                {
                    "request_index": 2,
                    "input_tokens": 15,
                    "max_output_tokens": 8,
                },
            )
        usage = get("admin", "admin/usage").json()
        assert [(item["name"], item["tokens"], item["requests"]) for item in usage] == [
            ("Owner", 44, 1)
        ]

        assistant = journal.record_assistant(
            Message(
                "assistant",
                "",
                tool_calls=(
                    ToolCall(
                        "review-test",
                        "create_file",
                        '{"path":"small.txt","content":"one\\n"}',
                    ),
                ),
            )
        )
        operation = journal.operation_id_for(assistant, "review-test")
        with db.scoped(engine, str(tenant)) as conn:
            conn.execute(
                text("""
                INSERT INTO sw.approvals (tenant_id,run_id,id,state,expires_at,preview,fingerprint)
                VALUES (:tenant,:run,:id,'pending',now()+interval '60 seconds','{}','fixture')
            """),
                {"tenant": tenant, "run": ids["run_id"], "id": operation},
            )
        assert (
            post("viewer", route + "/approvals/" + operation, {"decision": "approve"}).status_code
            == 403
        )
        assert (
            post("admin", route + "/approvals/" + operation, {"decision": "approve"}).status_code
            == 204
        )

        candidate = {
            "files": [
                {
                    "header": "diff --git a/small.txt b/small.txt\n",
                    "hunks": [
                        {"id": "a" * 64, "diff": "@@ -0,0 +1 @@\n+one\n"},
                    ],
                }
            ]
        }
        with db.scoped(engine, str(tenant)) as conn:
            conn.execute(
                text("""
                UPDATE sw.runs SET report=CAST(:report AS jsonb) WHERE id=:run
            """),
                {"run": ids["run_id"], "report": json.dumps({"review": candidate})},
            )
        patch = get("owner", route + "/patch")
        assert patch.status_code == 200 and "+one\n" in patch.text
        assert get("teammate", route + "/patch").status_code == 404
        assert post("admin", route + "/cancel", {}).status_code == 204
        assert get("owner", route + "/events").status_code == 200
        audit = get("admin", "admin/audit").json()
        assert {(row["name"], row["action"]) for row in audit} >= {
            ("Owner", "run.submit"),
            ("Operator", "approval.approve"),
            ("Operator", "run.cancel"),
            ("Owner", "report.read"),
        }
        assert get("other_project", "admin/audit").json() == []

        # Completed runs still count in the submission-rate window, while retries are free.
        with db.scoped(engine, str(tenant)) as conn:
            conn.execute(
                text("""
                INSERT INTO sw.runs(tenant_id,id,session_id,request_key,prompt,provider,mode,status,member_id)
                SELECT tenant_id, gen_random_uuid(), session_id,gen_random_uuid(),'fixture',
                    provider,mode,'completed',member_id
                FROM sw.runs CROSS JOIN generate_series(1,19) WHERE id=:run
            """),
                {"run": ids["run_id"]},
            )
        assert post("owner", "runs", request).json() == ids
        assert post("owner", "runs", run_body("owner")).status_code == 429
        assert post("teammate", "runs", run_body("teammate")).status_code == 202


@pytest.mark.postgres
def test_oidc_pkce_signed_session_and_revocation(engine, tmp_path: Path):
    issuer, origin = "http://127.0.0.1:9990", "http://127.0.0.1"
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signer.public_key()))
    jwk.update(kid="fixture", use="sig", alg="RS256")
    challenge = ""
    nonce = ""
    wrong_nonce = False

    def provider(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": issuer + "/authorize",
                    "token_endpoint": issuer + "/token",
                    "jwks_uri": issuer + "/jwks",
                },
            )
        if req.url.path == "/token":
            params = parse_qs(req.content.decode())
            computed = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(params["code_verifier"][0].encode()).digest()
                )
                .rstrip(b"=")
                .decode()
            )
            assert computed == challenge
            assert params["redirect_uri"] == [origin + CALLBACK]
            claims = {
                "iss": issuer,
                "sub": "registered-subject",
                "aud": "fixture-client",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "nonce": "invalid" if wrong_nonce else nonce,
            }
            return httpx.Response(
                200,
                json={
                    "id_token": jwt.encode(
                        claims,
                        signer,
                        algorithm="RS256",
                        headers={"kid": "fixture"},
                    )
                },
            )
        assert req.url.path == "/jwks"
        return httpx.Response(200, json={"keys": [jwk]})

    principal = account(uuid4(), tmp_path, "member", "OIDC fixture")
    oidc = OIDC(
        OIDCConfig(issuer, origin, "fixture-client", "fixture-secret", True),
        httpx.MockTransport(provider),
    )
    app = create_app(
        engine,
        {},
        tmp_path.parent / "isolated-state",
        queued=True,
        isolated=True,
        oidc=oidc,
        subjects={"registered-subject": principal},
    )
    with TestClient(app, base_url=origin, follow_redirects=False) as client:

        def begin():
            nonlocal challenge, nonce
            login = client.get("/api/v1/auth/login")
            assert login.status_code == 303
            params = parse_qs(urlsplit(login.headers["Location"]).query)
            assert params["code_challenge_method"] == ["S256"]
            challenge, nonce = params["code_challenge"][0], params["nonce"][0]
            return params["state"][0]

        state = begin()
        assert (
            client.get(CALLBACK, params={"state": "invalid", "code": "fixture"}).status_code == 400
        )
        wrong_nonce = True
        assert client.get(CALLBACK, params={"state": state, "code": "fixture"}).status_code == 400
        wrong_nonce = False
        state = begin()
        logged = client.get(CALLBACK, params={"state": state, "code": "fixture"})
        assert logged.status_code == 303
        assert "HttpOnly" in logged.headers.get_list("set-cookie")[-1]
        assert client.get("/api/v1/workspaces").json()["account"]["name"] == "OIDC fixture"
        assert client.get(CALLBACK, params={"state": state, "code": "fixture"}).status_code == 400
        cookie = client.cookies[SESSION_COOKIE]
        assert client.post("/api/v1/auth/logout").status_code == 403
        assert (
            client.post(
                "/api/v1/auth/logout",
                headers={
                    "X-StellarWeaver-Request": "1",
                    "Origin": issuer,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/auth/logout",
                headers={
                    "X-StellarWeaver-Request": "1",
                    "Origin": origin,
                },
            ).status_code
            == 204
        )
        assert (
            client.get(
                "/api/v1/workspaces",
                headers={
                    "Cookie": SESSION_COOKIE + "=" + cookie,
                },
            ).status_code
            == 401
        )
