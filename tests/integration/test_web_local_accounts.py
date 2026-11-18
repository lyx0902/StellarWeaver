"""Exercise local accounts and projects with an actual PostgreSQL queue."""

import os
import subprocess
import time
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from stellarweaver.instructions import InstructionLoader  # noqa: E402
from stellarweaver.web import db  # noqa: E402
from stellarweaver.web.app import LOCAL_TENANT, create_app  # noqa: E402
from stellarweaver.web.isolation import Isolation  # noqa: E402
from stellarweaver.web.local_accounts import LocalConfig  # noqa: E402
from stellarweaver.web.worker import run_team_worker  # noqa: E402

HEADERS = {"X-StellarWeaver-Request": "1"}


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


def completed(client: TestClient, run_id: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}")
        response.raise_for_status()
        row = response.json()
        if row["status"] != "active":
            assert row["status"] == "completed", row.get("outcome")
            return
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
