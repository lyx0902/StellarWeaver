"""Exercise local accounts and projects with an actual PostgreSQL queue."""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from stellarweaver.domain import AssistantReply, ToolCall  # noqa: E402
from stellarweaver.instructions import InstructionLoader  # noqa: E402
from stellarweaver.providers.fake import FakeModel  # noqa: E402
from stellarweaver.web import db  # noqa: E402
from stellarweaver.web.app import LOCAL_TENANT, create_app  # noqa: E402
from stellarweaver.web.isolation import DockerRunner, Isolation  # noqa: E402
from stellarweaver.web.local_accounts import LocalConfig  # noqa: E402
from stellarweaver.web.oidc import SESSION_COOKIE  # noqa: E402
from stellarweaver.web.worker import run_team_worker  # noqa: E402

HEADERS = {"X-StellarWeaver-Request": "1"}


def test_experiment_file_is_visible_only_to_initial_local_owner(tmp_path, monkeypatch):
    from stellarweaver.web import app as web_app

    owner, other_tenant = uuid4(), uuid4()
    config = LocalConfig(tmp_path, "D:/Projects", LOCAL_TENANT, owner, str(uuid4()), "legacy")
    identity = {"owner": (str(LOCAL_TENANT), str(owner)), "other": (str(other_tenant), str(owner))}
    monkeypatch.setattr(web_app, "session_member", lambda _, cookie: identity.get(cookie))
    monkeypatch.setattr(web_app.local_accounts, "lookup", lambda *_: "Local Admin")
    monkeypatch.setattr(web_app.local_accounts, "projects", lambda *_: [])
    report = tmp_path / "report.json"
    report.write_text(
        '{"schema_version":1,"instance_id":"trial","api_key":"fixture-secret","log":"Authorization: Bearer sensitive-log"}',
        encoding="utf-8",
    )
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "boundary.json").write_text(
        json.dumps(
            {
                "manifest": {
                    "schema_version": 1,
                    "suite": "local_fixtures",
                    "instance_ids": ["case-1"],
                    "fixture_sha256": "a" * 64,
                    "oracle_source_sha256": "b" * 64,
                    "model": "FakeModel",
                },
                "security": {"planned": 900},
                "cases": [
                    {
                        "instance_id": "case-1",
                        "kind": "protected",
                        "passed": True,
                        "unsafe_effect": False,
                        "tool_calls": 1,
                        "model_calls": 2,
                        "approvals": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (reports / "invalid.json").write_text("invalid", encoding="utf-8")
    app = create_app(
        Mock(), {}, tmp_path, queued=True, isolated=True, local=config, experiment_file=report
    )
    client = TestClient(app, base_url="http://127.0.0.1")

    assert client.get("/api/v1/experiment").status_code == 401
    assert client.get("/api/v1/experiments").status_code == 401
    client.cookies.set(SESSION_COOKIE, "other")
    assert client.get("/api/v1/workspaces").json()["experiment_available"] is False
    assert client.get("/api/v1/experiment").status_code == 404
    assert client.get("/api/v1/experiments/boundary").status_code == 404
    client.cookies.set(SESSION_COOKIE, "owner")
    assert client.get("/api/v1/workspaces").json()["experiment_available"] is True
    response = client.get("/api/v1/experiment")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["instance_id"] == "trial"
    assert "fixture-secret" not in response.text and "sensitive-log" not in response.text
    index = client.get("/api/v1/experiments").json()
    assert {row["kind"] for row in index["reports"]} == {"legacy", "security"}
    assert index["unavailable"] == ["invalid"]
    detail = client.get("/api/v1/experiments/boundary").json()["report"]
    assert detail["security"] == {
        "planned": 1,
        "boundary_checks_passed": 1,
        "unauthorized_effects": 0,
    }
    assert client.get("/api/v1/experiments/compare?left=boundary&right=legacy").status_code == 422
    report.write_text("invalid", encoding="utf-8")
    assert client.get("/api/v1/experiment").status_code == 503


def git_repo(root: Path, name: str) -> None:
    path = root / name
    path.mkdir()
    (path / "README.md").write_text(name, encoding="utf-8")
    for command in (
        ["init", "-q"],
        ["add", "README.md"],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "init",
        ],
    ):
        subprocess.run(["git", *command], cwd=path, check=True, capture_output=True)


def start(
    client: TestClient,
    workspace: str,
    message: str,
    session: str | None = None,
    *,
    mode: str = "read-only",
):
    return client.post(
        "/api/v1/runs",
        headers=HEADERS,
        json={
            "workspace_id": workspace,
            "request_id": str(uuid4()),
            "prompt": message,
            "session_id": session,
            "mode": mode,
        },
    )


def completed(client: TestClient, run_id: str, *, approve: bool = False):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        response.raise_for_status()
        row = response.json()
        if row["status"] != "active":
            assert row["status"] == "completed", row.get("outcome")
            return
        if approve and (pending := row.get("pending_approval")) is not None:
            decision = client.post(
                f"/api/v1/runs/{run_id}/approvals/{pending['id']}",
                headers=HEADERS,
                json={"decision": "approve"},
            )
            assert decision.status_code == 204, decision.text
        time.sleep(0.1)
    pytest.fail("Queued run did not complete")


@pytest.mark.postgres
def test_password_accounts_workspace_selection_archives_and_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dsn = os.getenv("STELLARWEAVER_LOCAL_TEST_DSN")
    if not dsn:
        pytest.skip("requires a dedicated test database for first-account enrollment")
    engine = db.engine_for(dsn)
    root, state = tmp_path / "src", tmp_path / "state"
    api_state = tmp_path / "api-state"
    root.mkdir()
    state.mkdir()
    load_instructions = InstructionLoader.for_paths

    def project_instructions(loader, paths):
        assert not loader.workspace.root.is_relative_to(state / "chat")
        return load_instructions(loader, paths)

    monkeypatch.setattr(InstructionLoader, "for_paths", project_instructions)
    for name in ("legacy", "second"):
        git_repo(root, name)
    tenant, old_workspace = str(LOCAL_TENANT), str(uuid4())
    db.register(engine, tenant, {old_workspace: "legacy"})
    legacy, _ = db.submit(
        engine, tenant, old_workspace, str(uuid4()), "old thread", "fake", isolated=True
    )
    with db.scoped(engine, tenant) as conn:
        conn.execute(
            text("UPDATE sw.runs SET status='completed' WHERE id=:run"), {"run": legacy["run_id"]}
        )
    config = LocalConfig(root, "D:/Projects", LOCAL_TENANT, uuid4(), old_workspace, "legacy")
    # Only the Worker receives the state volume in the deployed Compose stack.
    app = create_app(engine, {}, api_state, queued=True, isolated=True, local=config)
    stop = Event()
    worker = Thread(
        target=run_team_worker,
        args=(engine, {}, state),
        kwargs={
            "provider": "fake",
            "api_key": None,
            "enable_repair": False,
            "isolation": Isolation("fixture:local", (("python", "-m", "unittest"),), "fixture"),
            "remote": ("http://127.0.0.1:1", "fixture"),
            "slots": 2,
            "shutdown": stop,
            "local": config,
        },
    )
    worker.start()
    try:
        with (
            TestClient(app, base_url="http://127.0.0.1") as owner,
            TestClient(app, base_url="http://127.0.0.1") as other,
        ):
            assert owner.get("/api/v1/auth/config").json() == {
                "password": True,
                "bearer": False,
                "oidc": False,
            }
            credentials = {"username": "owner-test", "password": "local-fixture-password"}
            register = "/api/v1/auth/register"
            assert owner.post(register, json=credentials).status_code == 403
            enrolled = owner.post(register, headers=HEADERS, json=credentials)
            assert enrolled.status_code == 204, enrolled.text
            assert "HttpOnly" in enrolled.headers["set-cookie"]
            assert owner.get("/api/v1/runs/" + legacy["run_id"]).json()["can_continue"]
            assert (
                other.post(
                    register, headers=HEADERS, json={**credentials, "username": "OWNER-TEST"}
                ).status_code
                == 409
            )
            assert (
                other.post(
                    register, headers=HEADERS, json={**credentials, "username": "other-test"}
                ).status_code
                == 204
            )
            assert other.get("/api/v1/threads").json() == []
            assert other.get("/api/v1/runs/" + legacy["run_id"]).status_code == 404
            assert (
                other.patch(
                    "/api/v1/threads/" + legacy["session_id"],
                    headers=HEADERS,
                    json={"archived": True},
                ).status_code
                == 404
            )
            assert (
                owner.get("/api/v1/workspaces/folders", params={"path": "../outside"}).status_code
                == 404
            )
            folders = owner.get("/api/v1/workspaces/folders").json()["folders"]
            assert {item["name"] for item in folders} == {"legacy", "second"}
            for _ in range(2):
                assert (
                    owner.post(
                        "/api/v1/workspaces", headers=HEADERS, json={"path": "second"}
                    ).status_code
                    == 204
                )
            ws = owner.get("/api/v1/workspaces").json()["workspaces"]
            assert [entry["label"] for entry in ws] == ["legacy", "second", "普通对话"]
            assert ws[1]["path"] == "D:/Projects/second" and ws[-1]["kind"] == "chat"
            assert ws[1]["alias"] is None and ws[1]["default_label"] == "second"
            rename = f"/api/v1/workspaces/{ws[1]['id']}"
            assert owner.patch(rename, json={"alias": "blocked"}).status_code == 403
            assert (
                other.patch(rename, headers=HEADERS, json={"alias": "blocked"}).status_code == 404
            )
            assert (
                owner.patch(
                    f"/api/v1/workspaces/{ws[-1]['id']}", headers=HEADERS, json={"alias": "chat"}
                ).status_code
                == 404
            )
            for invalid in ("x" * 81, "two\nlines"):
                assert (
                    owner.patch(rename, headers=HEADERS, json={"alias": invalid}).status_code == 422
                )
            updated = owner.patch(rename, headers=HEADERS, json={"alias": "  我的实验  "})
            assert updated.status_code == 200
            assert updated.json() == {"alias": "我的实验", "label": "我的实验"}
            listed = owner.get("/api/v1/workspaces").json()["workspaces"][1]
            assert listed == {**ws[1], "alias": "我的实验", "label": "我的实验"}
            assert start(other, ws[1]["id"], "blocked project").status_code == 404
            assert start(other, ws[-1]["id"], "blocked chat").status_code == 404
            assert start(owner, ws[-1]["id"], "no tools", mode="repair").status_code == 422
            project = start(owner, ws[1]["id"], "project fixture")
            chat = start(owner, ws[-1]["id"], "chat fixture")
            assert project.status_code == chat.status_code == 202, (project.text, chat.text)
            completed(owner, project.json()["run_id"])
            chat_id = chat.json()["run_id"]
            session_id = chat.json()["session_id"]
            completed(owner, chat_id)
            chat_events = owner.get(f"/api/v1/runs/{chat_id}/events").text
            assert "tool.completed" not in chat_events
            assert "离线普通对话" in chat_events
            assert start(owner, ws[1]["id"], "wrong project", session_id).status_code == 409
            archive = f"/api/v1/threads/{session_id}"
            assert owner.patch(archive, headers=HEADERS, json={"archived": True}).status_code == 204
            assert next(
                row
                for row in owner.get("/api/v1/threads").json()
                if row["session_id"] == session_id
            )["archived"]
            assert start(owner, ws[-1]["id"], "archived", session_id).status_code == 409
            assert (
                owner.patch(archive, headers=HEADERS, json={"archived": False}).status_code == 204
            )
            followup = start(owner, ws[-1]["id"], "follow-up", session_id)
            assert followup.status_code == 202, followup.text
            assert followup.json()["session_id"] == session_id
            followup_id = followup.json()["run_id"]
            completed(owner, followup_id)
            followup_events = owner.get(f"/api/v1/runs/{followup_id}/events").text
            assert "离线普通对话" in followup_events
            assert "tool.completed" not in followup_events
            assert (state / "chat" / tenant / session_id).is_dir()
            assert not api_state.exists()
            (root / "second").rename(root / "moved-second")
            missing = start(owner, ws[1]["id"], "missing project")
            assert missing.status_code == 404
            assert missing.json()["detail"] == "该项目目录不可访问。"
            assert owner.post("/api/v1/auth/logout", headers=HEADERS).status_code == 204
            assert owner.get("/api/v1/workspaces").status_code == 401
            assert (
                owner.post(
                    "/api/v1/auth/login",
                    headers=HEADERS,
                    json={**credentials, "password": "incorrect password"},
                ).status_code
                == 401
            )
            assert (
                owner.post("/api/v1/auth/login", headers=HEADERS, json=credentials).status_code
                == 204
            )
            assert owner.get("/api/v1/workspaces").json()["account"]["name"] == "owner-test"
            persisted = owner.get("/api/v1/workspaces").json()["workspaces"][1]
            assert persisted["alias"] == "我的实验" and persisted["id"] == ws[1]["id"]
            assert owner.patch(rename, headers=HEADERS, json={"alias": " "}).json() == {
                "alias": None,
                "label": "second",
            }
            with engine.connect() as db_conn:
                value = db_conn.execute(
                    text("SELECT password_hash FROM sw.local_accounts WHERE username='owner-test'")
                ).scalar_one()
                assert credentials["password"] not in value and ":" in value
    finally:
        stop.set()
        worker.join(15)
        engine.dispose()
        assert not worker.is_alive()


@pytest.mark.postgres
def test_local_account_feedback_reverification_and_ide_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    image = os.getenv("STELLARWEAVER_G4_TEST_IMAGE")
    if not dsn or not image:
        pytest.skip("requires PostgreSQL and the Docker verification image")
    source_root = tmp_path / "src"
    source_root.mkdir()
    for name in ("legacy", "review-project"):
        git_repo(source_root, name)
    source = source_root / "review-project"
    state = tmp_path / "state"
    engine = db.engine_for(dsn)
    config = LocalConfig(source_root, "D:/Projects", uuid4(), uuid4(), str(uuid4()), "legacy")
    isolation = Isolation(
        image,
        (("python", "-c", "assert open('review.txt').read().strip() == 'revised'"),),
        f"test-{uuid4().hex}",
    )

    def model(_commands):
        def respond(request):
            prompt = next(msg.content for msg in reversed(request.messages) if msg.role == "user")
            last = request.messages[-1]
            revising = "Revise the note" in prompt
            if revising:
                assert '"path": "review.txt"' in prompt and '"hunk":' in prompt
            if last.role == "user":
                name = "read_file" if revising else "create_file"
                args = {"path": "review.txt"}
                if not revising:
                    args["content"] = "initial\n"
                return AssistantReply(tool_calls=(ToolCall("step-1", name, json.dumps(args)),))
            if revising and last.role == "tool" and last.tool_call_id == "step-1":
                args = {
                    "path": "review.txt",
                    "old_text": "initial",
                    "new_text": "revised",
                    "expected_sha256": json.loads(last.content)["data"]["sha256"],
                }
                return AssistantReply(
                    tool_calls=(ToolCall("step-2", "edit_file", json.dumps(args)),)
                )
            return AssistantReply("Review fixture finished")

        return FakeModel(respond)

    monkeypatch.setattr("stellarweaver.web.execution.offline_repair_model", model)
    monkeypatch.setattr(
        "stellarweaver.web.worker.RemoteRunner",
        lambda root, isolation, *_: DockerRunner(root, isolation),
    )
    app = create_app(
        engine,
        {},
        tmp_path / "api-state",
        queued=True,
        isolated=True,
        enable_repair=True,
        local=config,
    )
    stop = Event()
    worker = Thread(
        target=run_team_worker,
        args=(engine, {}, state),
        kwargs=dict(
            provider="fake",
            api_key=None,
            enable_repair=True,
            isolation=isolation,
            remote=("http://127.0.0.1:1", "fixture"),
            slots=1,
            shutdown=stop,
            local=config,
        ),
    )
    worker.start()
    try:
        with (
            TestClient(app, base_url="http://127.0.0.1") as owner,
            TestClient(app, base_url="http://127.0.0.1") as other,
        ):
            for client in (owner, other):
                registered = client.post(
                    "/api/v1/auth/register",
                    headers=HEADERS,
                    json={
                        "username": f"review-{uuid4().hex[:12]}",
                        "password": "local-fixture-password",
                    },
                )
                assert registered.status_code == 204, registered.text
            assert (
                owner.post(
                    "/api/v1/workspaces",
                    headers=HEADERS,
                    json={
                        "path": source.name,
                    },
                ).status_code
                == 204
            )
            workspace = next(
                w["id"]
                for w in owner.get("/api/v1/workspaces").json()["workspaces"]
                if w["label"] == source.name
            )

            def post(run_id, action, **body):
                return owner.post(
                    f"/api/v1/runs/{run_id}/review{action}", headers=HEADERS, json=body
                )

            def report(run_id):
                response = owner.get(f"/api/v1/runs/{run_id}/report")
                assert response.is_success, response.text
                return response.json()

            first = start(owner, workspace, "Create review note", mode="repair")
            assert first.status_code == 202, first.text
            original = first.json()
            original_id = original["run_id"]
            completed(owner, original_id, approve=True)
            original_hunk = report(original_id)["review"]["files"][0]["hunks"][0]["id"]
            body = {
                "request_id": str(uuid4()),
                "path": "review.txt",
                "hunk": original_hunk,
                "text": "Revise the note",
            }
            for action, payload in (
                ("/feedback", body),
                ("/preflight", {"hunks": [original_hunk]}),
                ("", {"request_id": str(uuid4()), "hunks": [original_hunk]}),
            ):
                denied = other.post(
                    f"/api/v1/runs/{original_id}/review{action}", headers=HEADERS, json=payload
                )
                assert denied.status_code == 404
            assert post(original_id, "/feedback", **{**body, "hunk": "0" * 64}).status_code == 422
            feedback = post(original_id, "/feedback", **body)
            assert feedback.status_code == 202, feedback.text
            revised = feedback.json()
            assert revised["session_id"] == original["session_id"]
            completed(owner, revised["run_id"], approve=True)
            assert post(original_id, "/feedback", **body).json() == revised
            assert (
                post(original_id, "/feedback", **{**body, "request_id": str(uuid4())}).status_code
                == 409
            )
            revised_patch = report(revised["run_id"])["review"]
            assert "+revised" in revised_patch["files"][0]["hunks"][0]["diff"]
            selected = [revised_patch["files"][0]["hunks"][0]["id"]]
            before = post(revised["run_id"], "/preflight", hunks=selected)
            assert before.status_code == 200, before.text
            assert before.json()["status"] == "ready" and not before.json()["handoff_ready"]
            review = post(revised["run_id"], "", request_id=str(uuid4()), hunks=selected)
            assert review.status_code == 202, review.text
            verified_id = review.json()["run_id"]
            assert review.json()["session_id"] != original["session_id"]
            completed(owner, verified_id)
            verified = report(verified_id)
            assert verified["verification"] == "passed", verified
            hunks = [verified["review"]["files"][0]["hunks"][0]["id"]]
            (source / "README.md").write_text("unrelated local edit", encoding="utf-8")
            result = post(verified_id, "/preflight", hunks=hunks).json()
            assert result["handoff_ready"], result
            assert result["branch"] and result["target_head"] == result["source_base"]
            patch = owner.get(f"/api/v1/runs/{verified_id}/patch").content
            assert hashlib.sha256(patch).hexdigest() == result["patch_sha256"]
            assert not (source / "review.txt").exists()
            assert (source / "README.md").read_text() == "unrelated local edit"

            (source / "review.txt").write_text("local change", encoding="utf-8")
            blocked = post(verified_id, "/preflight", hunks=hunks).json()
            assert blocked["status"] == "local_changes" and blocked["local_changes"] == [
                "review.txt"
            ]
            assert not blocked["handoff_ready"]
            subprocess.run(["git", "add", "review.txt"], cwd=source, check=True)
            (source / "review.txt").unlink()
            staged = post(verified_id, "/preflight", hunks=hunks).json()
            assert staged["local_changes"] == ["review.txt"] and not staged["handoff_ready"]
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "diverging target",
                ],
                cwd=source,
                check=True,
            )
            (source / "review.txt").write_text("local change", encoding="utf-8")
            conflict = post(verified_id, "/preflight", hunks=hunks).json()
            assert conflict["status"] == "conflict" and conflict["conflict_paths"] == [
                "review.txt"
            ], conflict
            assert (
                conflict["changed_since_base"] == ["review.txt"] and not conflict["handoff_ready"]
            )
    finally:
        stop.set()
        worker.join(15)
        engine.dispose()
        assert not worker.is_alive()
