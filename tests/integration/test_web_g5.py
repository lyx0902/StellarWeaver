"""G5 authorization, per-member accounting, and signed OIDC acceptance."""

import base64
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path
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
from stellarweaver.web import db  # noqa: E402
from stellarweaver.web.app import Principal, create_app  # noqa: E402
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
def test_team_access_quotas_audit_and_report_exports(engine, tmp_path: Path):
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    tenant = uuid4()
    accounts = {
        "owner": account(tenant, repo, "member", "Owner", active_limit=1, daily_tokens=65),
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
            for suffix in ("", "/events", "/report", "/patch"):
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
        with pytest.raises(BudgetExceeded, match="DAILY_MODEL_QUOTA"):
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
