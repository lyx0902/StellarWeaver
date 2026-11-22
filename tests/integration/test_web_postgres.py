"""Opt-in integration against a real, migrated database as the app role."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: E402

from stellarweaver.domain import AssistantReply, Message, ToolCall, ToolResult  # noqa: E402
from stellarweaver.providers.base import ProviderError  # noqa: E402
from stellarweaver.providers.fake import FakeModel  # noqa: E402
from stellarweaver.tools.workspace import sha256_bytes  # noqa: E402
from stellarweaver.web.app import LOCAL_TENANT, Principal, create_app  # noqa: E402
from stellarweaver.web.approval import VERIFY_ARGV, DecisionConflict, request_cancel  # noqa: E402
from stellarweaver.web.db import claim, engine_for, register, scoped, service_lock, submit  # noqa: E402
from stellarweaver.web.delivery import Delivery  # noqa: E402
from stellarweaver.web.identity import Member  # noqa: E402
from stellarweaver.web.isolation import Isolation, saved_worktree  # noqa: E402
from stellarweaver.web.journal import JobOwnershipLost, PgJournal, pack  # noqa: E402
from stellarweaver.web.offline import BAD, FIXED, WRONG  # noqa: E402
from stellarweaver.web.worker import run_worker  # noqa: E402


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def wait_done(client: TestClient, token: str, run_id: str) -> dict:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/runs/{run_id}", headers=auth(token))
        assert response.status_code == 200
        run = response.json()
        if run["status"] != "active":
            return run
        time.sleep(0.05)
    pytest.fail("Agent did not reach a terminal state")


def wait_approval(client: TestClient, token: str, path: str) -> dict:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        inspection = client.get(path, headers=auth(token)).json()
        assert inspection["status"] == "active", inspection
        if approval := inspection.get("pending_approval"):
            return approval
        time.sleep(0.05)
    pytest.fail("Agent did not request approval")


def test_passing_check_becomes_stale_after_later_edit(tmp_path: Path) -> None:
    source = tmp_path / "calc.py"
    source.write_text("import unittest\n", encoding="utf-8")
    delivery = Delivery(tmp_path, tmp_path / "state")
    delivery.begin()
    payload = {"tool": "run_command", "operation_id": "test-step"}
    delivery.started(payload)
    empty = subprocess.run(VERIFY_ARGV, cwd=tmp_path, capture_output=True, text=True, check=False)
    result = ToolResult(
        "test",
        "test-step",
        "run_command",
        "error" if empty.returncode else "ok",
        "",
        {"argv": VERIFY_ARGV, "exit_code": empty.returncode, "stderr": empty.stderr},
    )
    outcome = {"run_status": "completed"}
    delivery.result(result)
    assert delivery.report("run", outcome)["verification"] == "unconfirmed"

    (tmp_path / "test_calc.py").write_text(
        "import unittest\n\nclass Example(unittest.TestCase):\n"
        "    def test_example(self):\n        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    delivery.started(payload)
    tested = subprocess.run(VERIFY_ARGV, cwd=tmp_path, capture_output=True, text=True, check=True)
    delivery.result(
        ToolResult(
            "test",
            "test-step",
            "run_command",
            "ok",
            "",
            {"argv": VERIFY_ARGV, "exit_code": tested.returncode, "stderr": tested.stderr},
        )
    )
    assert delivery.report("run", outcome)["verification"] == "passed"
    source.write_text("import unittest\n# later edit\n", encoding="utf-8")
    report = delivery.report("run", outcome)
    assert report["verification"] == "stale"
    assert report["checks"][-1]["observed"] == "passed"


def test_journal_packing_renders_nul_and_preserves_literal_escape_sequences() -> None:
    original = {"nested": [{"stdout": "?? tracked.txt\x00"}, r"\u0000", "正常"]}
    assert json.loads(pack(original)) == {
        "nested": [{"stdout": "?? tracked.txt␀"}, r"\u0000", "正常"]
    }


def test_bound_workspace_identity_distinguishes_host_repositories(tmp_path: Path) -> None:
    tenant = uuid4()
    first = Principal(tenant, (tmp_path,), "D:/checkouts/project-a")
    second = Principal(tenant, (tmp_path,), "D:/checkouts/project-b")
    assert first.workspace_map() != second.workspace_map()
    assert first.workspace_label(tmp_path) == "project-a"


@pytest.mark.postgres
def test_static_workbench_and_redacted_model_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    engine = engine_for(dsn)
    workspace, static = tmp_path / "repo", tmp_path / "static"
    workspace.mkdir()
    static.mkdir()
    (static / "index.html").write_text("<h1>Packaged Workbench</h1>", encoding="utf-8")
    token = uuid4().hex
    principal = Principal(uuid4(), (workspace,))
    private = "provider-internal-diagnostic-marker"

    def fail(_request):
        raise ProviderError(private, status_code=401, retryable=False)

    monkeypatch.setattr(
        "stellarweaver.web.execution.offline_repository_model", lambda: FakeModel(fail)
    )
    try:
        with TestClient(
            create_app(engine, {token: principal}, tmp_path / "state", static_dir=static),
            base_url="http://127.0.0.1",
        ) as client:
            assert client.get("/healthz").json() == {"status": "ok"}
            assert "Packaged Workbench" in client.get("/?run=fixture").text
            assert client.get("/api/v1/runs").status_code == 401
            response = client.post(
                "/api/v1/runs",
                headers=auth(token),
                json={
                    "workspace_id": next(iter(principal.workspace_map())),
                    "request_id": str(uuid4()),
                    "prompt": "check failure",
                },
            )
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            status = wait_done(client, token, run_id)
            assert status["status"] == "failed"
            assert status["outcome"]["failure_phase"] == "model"
            stream = client.get(f"/api/v1/runs/{run_id}/events", headers=auth(token)).text
            assert "MODEL_REQUEST_FAILED" in stream and '"status_code": 401' in stream
            assert private not in stream and private not in str(status)

            def unavailable(*_args):
                raise OperationalError(None, None, RuntimeError(private))

            with monkeypatch.context() as patch:
                patch.setattr("stellarweaver.web.db.recent", unavailable)
                response = client.get("/api/v1/runs", headers=auth(token))
            assert response.status_code == 503 and private not in response.text
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("queued", [False, True], ids=["inline", "worker"])
def test_workbench_agent_loads_configured_skill_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: bool
) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")

    workspace = (tmp_path / "repo").resolve()
    skills = tmp_path / "configured-skills"
    name = "workbench-fixture"
    body = "WORKBENCH_SKILL_BODY"
    configured = skills / "workflow" / "SKILL.md"
    project = workspace / ".stellarweaver" / "skills" / "not-configured" / "SKILL.md"
    for path, key, contents in (
        (configured, name, body),
        (project, "workspace-untrusted", "DO_NOT_AUTO_LOAD"),
    ):
        path.parent.mkdir(parents=True)
        path.write_text(
            f"---\nname: {key}\ndescription: Guidance for the fixture\n---\n{contents}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("SW_WEB_AGENT_SKILLS_ROOT", str(skills))
    monkeypatch.delenv("SW_WEB_NESTED_SKILLS_ROOT", raising=False)
    monkeypatch.delenv("SW_WEB_CODEX_SKILLS_ROOT", raising=False)
    monkeypatch.delenv("SW_WEB_CONTEXT_WINDOW_TOKENS", raising=False)

    def respond(request):
        assert any(tool["function"]["name"] == "load_skill" for tool in request.tools)
        if request.messages[-1].role == "user":
            context = "\n".join(message.content for message in request.messages)
            assert name in context and "Guidance for the fixture" in context
            assert not any(value in context for value in (body, "workspace-untrusted", str(skills)))
            return AssistantReply(
                tool_calls=(ToolCall("skill", "load_skill", json.dumps({"name": name})),),
                stop_reason="tool_calls",
            )
        result = json.loads(request.messages[-1].content)
        assert body in result["content"]
        assert result["data"]["source_path"] == "workflow/SKILL.md"
        assert result["data"]["source_kind"] == "Agents"
        return AssistantReply("Used the configured skill.")

    monkeypatch.setattr(
        "stellarweaver.web.execution.offline_repository_model", lambda: FakeModel(respond)
    )
    engine = engine_for(dsn)
    principal, token = Principal(uuid4(), (workspace,)), uuid4().hex
    state = tmp_path / "state"
    try:
        with TestClient(
            create_app(
                engine,
                {token: principal},
                state,
                queued=queued,
                skill_roots=[("Agents", skills)],
            ),
            base_url="http://127.0.0.1",
        ) as client:
            headers = auth(token)
            assert (
                client.get("/api/v1/workspaces", headers=headers).json()["context_window_tokens"]
                == 200_000
            )
            catalog = client.get("/api/v1/skills", headers=headers).json()
            assert [skill["name"] for skill in catalog["skills"]] == [name]
            response = client.post(
                "/api/v1/runs",
                headers=headers,
                json={
                    "workspace_id": next(iter(principal.workspace_map())),
                    "request_id": str(uuid4()),
                    "mode": "read-only",
                    "prompt": "Use relevant workflow guidance.",
                },
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            if queued:
                pending = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
                assert pending["context_window_tokens"] == 200_000 and pending["status"] == "active"
                # The submitted window takes precedence over the worker's current default.
                monkeypatch.setenv("SW_WEB_CONTEXT_WINDOW_TOKENS", "120000")
                run_worker(
                    engine,
                    str(principal.tenant),
                    principal.workspace_map(),
                    state,
                    provider="fake",
                    once=True,
                )
            terminal = wait_done(client, token, run_id)
            assert terminal["status"] == "completed", terminal.get("outcome")
            assert terminal["context_window_tokens"] == 200_000
            stream = client.get(f"/api/v1/runs/{run_id}/events", headers=headers).text
            assert '"context_window_tokens": 200000' in stream
            assert '"skill_name": "workbench-fixture"' in stream
            assert '"skill_source": "Agents"' in stream
            assert "Used the configured skill." in stream and body not in stream
            exported = client.get(f"/api/v1/runs/{run_id}/export", headers=headers).json()
            assert exported["run"]["context_window_tokens"] == 200_000
            assert any(
                item["type"] == "model.started"
                and item["payload"]["context_window_tokens"] == 200_000
                for item in exported["events"]
            )
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_workbench_pins_project_skill_restores_evidence_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    workspace = tmp_path / "repo"
    skill = workspace / ".stellarweaver" / "skills" / "fixture" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: project-fixture\ndescription: Fixture workflow\n---\nSKILL_BODY_SENTINEL\n",
        encoding="utf-8",
    )
    source = workspace / "note.txt"
    source.write_text("FILE_BODY_SENTINEL", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "note.txt"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=workspace,
        check=True,
    )
    model_inputs = []

    def reply(request):
        model_inputs.append("\n".join(message.content for message in request.messages))
        if len(model_inputs) == 1:
            return AssistantReply(
                tool_calls=(ToolCall("read", "read_file", '{"path":"note.txt"}'),),
                stop_reason="tool_calls",
            )
        return AssistantReply("Done. password=fixture-secret <script>alert(1)</script>")

    monkeypatch.setattr(
        "stellarweaver.web.execution.offline_repository_model", lambda: FakeModel(reply)
    )
    tenant = uuid4()
    main = Member(id=uuid4(), tenant=tenant, name="owner", role="member", workspace_indexes=[0])
    other = Member(id=uuid4(), tenant=tenant, name="other", role="member", workspace_indexes=[0])
    principal = Principal(tenant, (workspace,), member=main)
    token, other_token = uuid4().hex, uuid4().hex
    workspace_id = next(iter(principal.workspace_map()))
    engine = engine_for(dsn)
    state = tmp_path / "state"
    isolation = Isolation("unused", (tuple(VERIFY_ARGV),), "parity-fixture")
    monkeypatch.setattr(Isolation, "reap", lambda *args: None)
    try:
        with TestClient(
            create_app(
                engine,
                {token: principal, other_token: Principal(tenant, (workspace,), member=other)},
                state,
                queued=True,
                isolated=True,
            ),
            base_url="http://127.0.0.1",
        ) as client:

            def catalog(key):
                return client.get(
                    "/api/v1/skills", params={"workspace_id": workspace_id}, headers=auth(key)
                ).json()

            def launch(prompt, **options):
                return client.post(
                    "/api/v1/runs",
                    headers=auth(token),
                    json={
                        "workspace_id": workspace_id,
                        "request_id": str(uuid4()),
                        "prompt": prompt,
                        **options,
                    },
                )

            def execute_one(ids):
                run_worker(
                    engine,
                    str(tenant),
                    principal.workspace_map(),
                    state,
                    provider="fake",
                    once=True,
                    isolation=isolation,
                )
                assert wait_done(client, token, ids["run_id"])["status"] == "completed"

            assert catalog(token)["skills"] == []
            trust = f"/api/v1/workspaces/{workspace_id}/skill-trust"
            assert client.put(trust, headers=auth(token), json={"enabled": True}).json() == {
                "project_trusted": True,
            }
            (entry,) = catalog(token)["skills"]
            assert entry["source"] == "项目" and catalog(other_token)["skills"] == []
            refs = [{key: entry[key] for key in ("id", "sha256")}]
            assert launch("Mismatch", skills=[{**refs[0], "sha256": "0" * 64}]).status_code == 409
            first = launch("Initial task", skills=refs)
            assert first.status_code == 202, first.text
            ids = first.json()
            execute_one(ids)
            assert "SKILL_BODY_SENTINEL" in model_inputs[0]
            route = f"/api/v1/runs/{ids['run_id']}"
            bundle_response = client.get(route + "/export", headers=auth(token))
            assert bundle_response.status_code == 200, bundle_response.text
            bundle = bundle_response.json()
            assert bundle["operations"][0]["tool_name"] == "read_file"
            assert bundle["events"][-1]["type"] == "run.completed"
            assert all(
                mark not in bundle_response.text
                for mark in (
                    "fixture-secret",
                    "SKILL_BODY_SENTINEL",
                    "FILE_BODY_SENTINEL",
                    "reasoning_content",
                )
            )
            page = client.get(route + "/export?format=html", headers=auth(token))
            assert "&lt;script&gt;" in page.text and "<script>" not in page.text
            assert client.get(route + "/export", headers=auth(other_token)).status_code == 404
            next_turn = launch("Follow up", session_id=ids["session_id"])
            assert next_turn.status_code == 202, next_turn.text
            execute_one(next_turn.json())
            assert '"matches_current": true' in model_inputs[-1]
            assert "SKILL_BODY_SENTINEL" in model_inputs[-1]
            work = saved_worktree(state, str(tenant), ids["session_id"])
            (work / "note.txt").write_text("changed outside the run", encoding="utf-8")
            changed = launch("Check freshness", session_id=ids["session_id"], skills=[])
            assert changed.status_code == 202, changed.text
            execute_one(changed.json())
            assert '"matches_current": false' in model_inputs[-1]
            assert "SKILL_BODY_SENTINEL" not in model_inputs[-1]
            assert (
                client.put(trust, headers=auth(token), json={"enabled": False}).status_code == 200
            )
            assert catalog(token)["skills"] == []
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_workbench_undo_restores_and_guards_real_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    base = b"before\n"
    (workspace / "note.txt").write_bytes(base)
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "note.txt"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=workspace,
        check=True,
    )
    state = tmp_path / "state"
    token, principal = uuid4().hex, Principal(uuid4(), (workspace,))
    isolation = Isolation("unused", (tuple(VERIFY_ARGV),), "parity-fixture")
    monkeypatch.setattr(Isolation, "reap", lambda *args: None)
    monkeypatch.setattr(
        "stellarweaver.web.execution.offline_repair_model",
        lambda _: FakeModel(
            [
                AssistantReply(
                    tool_calls=(
                        ToolCall(
                            "edit",
                            "edit_file",
                            json.dumps(
                                {
                                    "path": "note.txt",
                                    "old_text": "before",
                                    "new_text": "agent",
                                    "expected_sha256": sha256_bytes(base),
                                }
                            ),
                        ),
                    ),
                    stop_reason="tool_calls",
                ),
                AssistantReply(
                    tool_calls=(
                        ToolCall("create", "create_file", '{"path":"new.txt","content":"created"}'),
                    ),
                    stop_reason="tool_calls",
                ),
                AssistantReply("wrote two files"),
            ]
        ),
    )
    engine = engine_for(dsn)
    try:
        with TestClient(
            create_app(
                engine,
                {token: principal},
                state,
                queued=True,
                isolated=True,
                enable_repair=True,
            ),
            base_url="http://127.0.0.1",
        ) as client:

            def execute_one():
                run_worker(
                    engine,
                    str(principal.tenant),
                    principal.workspace_map(),
                    state,
                    provider="fake",
                    once=True,
                    enable_repair=True,
                    isolation=isolation,
                )

            first = client.post(
                "/api/v1/runs",
                headers=auth(token),
                json={
                    "workspace_id": next(iter(principal.workspace_map())),
                    "request_id": str(uuid4()),
                    "mode": "auto",
                    "prompt": "Edit and create",
                },
            )
            assert first.status_code == 202, first.text
            ids = first.json()
            execute_one()
            assert wait_done(client, token, ids["run_id"])["status"] == "completed"
            work = saved_worktree(state, str(principal.tenant), ids["session_id"])
            assert (work / "note.txt").read_bytes() == b"agent\n" and (work / "new.txt").is_file()

            def undo(previous, path):
                route = f"/api/v1/runs/{previous}/undo"
                result = client.get(route, headers=auth(token))
                assert result.status_code == 200, result.text
                target = result.json()["target"]
                assert target["path"] == path
                payload = {"operation_id": target["operation_id"], "request_id": str(uuid4())}
                accepted = client.post(route, headers=auth(token), json=payload)
                assert accepted.status_code == 202, accepted.text
                assert (
                    client.post(route, headers=auth(token), json=payload).json() == accepted.json()
                )
                execute_one()
                return wait_done(client, token, accepted.json()["run_id"])

            removed = undo(ids["run_id"], "new.txt")
            assert removed["status"] == "completed" and not (work / "new.txt").exists()
            (work / "note.txt").write_bytes(b"user concurrent change\n")
            blocked = undo(removed["run_id"], "note.txt")
            assert blocked["outcome"]["error_code"] == "UNDO_CONFLICT"
            assert (work / "note.txt").read_bytes() == b"user concurrent change\n"
            (work / "note.txt").write_bytes(b"agent\n")
            restored = undo(blocked["run_id"], "note.txt")
            assert restored["status"] == "completed" and (work / "note.txt").read_bytes() == base
            assert (workspace / "note.txt").read_bytes() == base and not (
                workspace / "new.txt"
            ).exists()
            original_report = client.get(
                f"/api/v1/runs/{ids['run_id']}/report", headers=auth(token)
            ).json()
            assert {entry["path"] for entry in original_report["patches"]} == {
                "note.txt",
                "new.txt",
            }
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("after_edit", [False, True])
def test_abrupt_worker_exit_does_not_repeat_approved_edit(tmp_path: Path, after_edit: bool) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    engine = engine_for(dsn)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    src = root / "calc.py"
    old = "def add(a, b):\n    " + BAD + "\n"
    src.write_text(old, encoding="utf-8")
    state = tmp_path / "state"
    token = uuid4().hex
    principal = Principal(LOCAL_TENANT, (root,))
    command = [
        sys.executable,
        "-B",
        "-m",
        "stellarweaver.web",
        "worker",
        "--once",
        "--provider",
        "fake",
        "--enable-repair",
        "--workspace",
        str(root),
        "--state-dir",
        str(state),
    ]
    env = {**os.environ, "STELLARWEAVER_DATABASE_DSN": dsn}

    def kill_worker(process: subprocess.Popen) -> None:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=True,
                    timeout=12,
                )
            else:
                process.kill()
        process.wait(timeout=12)

    try:
        with (
            TestClient(
                create_app(engine, {token: principal}, state, queued=True, enable_repair=True),
                base_url="http://127.0.0.1",
            ) as client,
            (tmp_path / "worker.log").open("wb") as log,
        ):
            response = client.post(
                "/api/v1/runs",
                headers=auth(token),
                json={
                    "workspace_id": next(iter(principal.workspace_map())),
                    "request_id": str(uuid4()),
                    "prompt": "Exercise interrupted approval",
                    "mode": "repair",
                },
            )
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            route = f"/api/v1/runs/{run_id}"
            process = subprocess.Popen(
                command,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            try:
                approval = wait_approval(client, token, route)
                assert approval["tool"] == "edit_file"
                if after_edit:
                    assert (
                        client.post(
                            route + "/approvals/" + approval["id"],
                            headers=auth(token),
                            json={"decision": "approve"},
                        ).status_code
                        == 204
                    )
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        request = (
                            client.get(route, headers=auth(token)).json().get("pending_approval")
                        )
                        if request and request["tool"] == "run_command":
                            break
                        time.sleep(0.05)
                    else:
                        pytest.fail("Worker did not reach the next approval")
                expected = old.replace(BAD, WRONG) if after_edit else old
                assert src.read_text() == expected
            finally:
                kill_worker(process)
            subprocess.run(command, env=env, check=True, capture_output=True, timeout=20)
            done = wait_done(client, token, run_id)
            assert done["status"] == "interrupted"
            assert done["outcome"]["failure_phase"] == "approval"
            assert done["outcome"]["unfinished_tools"] == 0
            assert done["outcome"]["recorded_results"] == (2 if after_edit else 1)
            assert src.read_text() == expected
            events = client.get(route + "/events", headers=auth(token)).text
            assert events.count('"type": "run.claimed"') == 1
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_queue_survives_api_restart_and_executes_once_in_worker_process(tmp_path: Path) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    engine = engine_for(dsn)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "queue-worker-fixture.txt").write_text("real file")
    token = uuid4().hex
    principal = Principal(LOCAL_TENANT, (root,))
    state = tmp_path / "state"
    command = [
        sys.executable,
        "-B",
        "-m",
        "stellarweaver.web",
        "worker",
        "--once",
        "--provider",
        "fake",
        "--workspace",
        str(root),
        "--state-dir",
        str(state),
    ]
    environment = {**os.environ, "STELLARWEAVER_DATABASE_DSN": dsn}
    environment.pop("STELLARWEAVER_WEB_TOKEN", None)
    body = {
        "workspace_id": next(iter(principal.workspace_map())),
        "request_id": str(uuid4()),
        "prompt": "Inspect the real queue fixture",
    }
    headers = auth(token)
    try:
        with TestClient(
            create_app(engine, {token: principal}, state, queued=True), base_url="http://127.0.0.1"
        ) as api:
            response = api.post("/api/v1/runs", headers=headers, json=body)
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            path = f"/api/v1/runs/{run_id}"
            queued = api.get(path, headers=headers).json()
            assert queued["job_state"] == "queued" and queued["through_seq"] == 0
        with TestClient(
            create_app(engine, {token: principal}, state, queued=True), base_url="http://127.0.0.1"
        ) as restarted:
            assert restarted.get(path, headers=headers).json() == queued
            assert (
                restarted.post("/api/v1/runs", headers=headers, json=body).json() == response.json()
            )
            cancelled = restarted.post(
                "/api/v1/runs", headers=headers, json={**body, "request_id": str(uuid4())}
            ).json()
            cancel_path = f"/api/v1/runs/{cancelled['run_id']}"
            assert restarted.post(cancel_path + "/cancel", headers=headers).status_code == 204
            assert restarted.get(cancel_path, headers=headers).json()["status"] == "cancelled"
            with service_lock(engine, queued=True, worker=True):
                duplicate = subprocess.run(
                    command, env=environment, capture_output=True, text=True, timeout=20
                )
                assert duplicate.returncode != 0 and "Another process owns" in duplicate.stderr
            for _ in range(2):
                result = subprocess.run(
                    command, env=environment, capture_output=True, text=True, timeout=20
                )
                assert result.returncode == 0, result.stderr
            done = restarted.get(path, headers=headers).json()
            assert done["status"] == "completed" and done["job_state"] == "done"
            stream = restarted.get(path + "/events", headers=headers).text
            assert stream.count('"type": "run.claimed"') == 1
            assert stream.count('"type": "tool.completed"') == 1
            assert "queue-worker-fixture.txt" in stream
        with scoped(engine, str(LOCAL_TENANT)) as conn:
            states = dict(conn.execute(text("SELECT run_id,attempt FROM sw.run_jobs")).all())
            assert {str(key): value for key, value in states.items()}[run_id] == 1
            assert {str(key): value for key, value in states.items()}[cancelled["run_id"]] == 0
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_queued_approval_survives_api_restart_and_cancel_reaches_worker(tmp_path: Path) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    engine = engine_for(dsn)
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    source = root / "calc.py"
    broken = "def add(a, b):\n    " + BAD + "\n"
    source.write_text(broken, encoding="utf-8")
    state = tmp_path / "state"
    token = uuid4().hex
    principal = Principal(uuid4(), (root,))
    tenant = str(principal.tenant)
    worker = None

    def api() -> TestClient:
        return TestClient(
            create_app(engine, {token: principal}, state, queued=True, enable_repair=True),
            base_url="http://127.0.0.1",
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            with api() as client:
                response = client.post(
                    "/api/v1/runs",
                    headers=auth(token),
                    json={
                        "workspace_id": next(iter(principal.workspace_map())),
                        "request_id": str(uuid4()),
                        "prompt": "Inspect the approval fixture",
                        "mode": "repair",
                    },
                )
                assert response.status_code == 202
                ids = response.json()
                path = f"/api/v1/runs/{ids['run_id']}"
                worker = pool.submit(
                    run_worker,
                    engine,
                    tenant,
                    principal.workspace_map(),
                    state,
                    provider="fake",
                    enable_repair=True,
                    once=True,
                )
                approval = wait_approval(client, token, path)
            with api() as restarted:
                current = restarted.get(path, headers=auth(token)).json()
                assert current["job_state"] == "running"
                assert current["pending_approval"] == approval
                assert restarted.post(path + "/cancel", headers=auth(token)).status_code == 204
                assert wait_done(restarted, token, ids["run_id"])["status"] == "cancelled"
                decision = restarted.post(
                    path + "/approvals/" + approval["id"],
                    headers=auth(token),
                    json={"decision": "approve"},
                )
                assert decision.status_code == 409
                assert source.read_text() == broken
        finally:
            if worker is not None:
                if not worker.done():
                    journal = PgJournal(
                        engine, tenant, ids["session_id"], ids["run_id"], root, state
                    )
                    with suppress(DecisionConflict):
                        request_cancel(journal)
                worker.result(timeout=20)
            engine.dispose()


@pytest.mark.postgres
def test_claim_contenders_and_recovery_leave_started_operation_unconfirmed(tmp_path: Path) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    engine = engine_for(dsn)
    principal = Principal(uuid4(), (tmp_path,))
    tenant = str(principal.tenant)
    workspace = next(iter(principal.workspace_map()))
    owners = [str(uuid4()), str(uuid4())]
    try:
        register(engine, tenant, {workspace: "Recovery fixture"})
        ids, _ = submit(engine, tenant, workspace, str(uuid4()), "Read", "fake", enqueue=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda owner: claim(engine, tenant, owner), owners))
        assert sum(item is not None for item in claims) == 1
        owner = owners[0] if claims[0] is not None else owners[1]
        journal = PgJournal(
            engine,
            tenant,
            ids["session_id"],
            ids["run_id"],
            tmp_path,
            tmp_path / "state",
            claim=(owner, 1),
        )
        call = ToolCall("unresolved", "list_files", "{}")
        assistant = journal.record_assistant(Message("assistant", "", tool_calls=(call,)))
        operation = journal.operation_id_for(assistant, call.call_id)
        invocation = {"operation_id": operation, "call_id": call.call_id, "tool": call.name}
        journal.record_approved(assistant, {**invocation, "decision": "allow"})
        journal.record_intent(assistant, invocation)
        run_worker(
            engine,
            tenant,
            principal.workspace_map(),
            tmp_path / "state",
            provider="fake",
            once=True,
        )
        with pytest.raises(JobOwnershipLost):
            journal.append_event("stale.worker", {})
        with scoped(engine, tenant) as conn:
            state = conn.execute(text("SELECT status,outcome FROM sw.runs")).one()
            assert state.status == "interrupted"
            assert state.outcome["verification_status"] == "unknown"
            assert conn.execute(text("SELECT attempt FROM sw.run_jobs")).scalar_one() == 1
            assert conn.execute(text("SELECT state FROM sw.operations")).scalar_one() == "started"
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize(
    "case", ["success", "deny", "hash_conflict", "expiry", "cancel", "stale", "test_cancel"]
)
def test_local_repair_approval_and_evidence(tmp_path: Path, case: str) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    engine = engine_for(dsn)
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    source = root / "calc.py"
    broken = "def add(a, b):\n    " + BAD + "\n"
    source.write_text(broken, encoding="utf-8")
    preamble = "import unittest\nfrom calc import add\n"
    if case == "test_cancel":
        preamble += (
            "import time\nfrom pathlib import Path\n"
            "Path('.test-started').touch()\ntime.sleep(60)\nPath('.test-finished').touch()\n"
        )
    test_file = root / "test_calc.py"
    test_file.write_text(
        preamble + "class AddTest(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    if case == "stale":
        # The test mutates another observed worktree file while returning success.
        with test_file.open("a", encoding="utf-8") as stream:
            stream.write(
                "        from pathlib import Path\n"
                "        Path('late-change.txt').write_text('after input snapshot')\n"
            )
    ta, tb = uuid4().hex, uuid4().hex
    principals = {ta: Principal(uuid4(), (root,)), tb: Principal(uuid4(), (root,))}
    headers = auth(ta)
    body = {
        "workspace_id": next(iter(principals[ta].workspace_map())),
        "request_id": str(uuid4()),
        "prompt": "Repair the offline exercise",
        "mode": "repair",
    }
    try:
        with TestClient(
            create_app(
                engine,
                principals,
                tmp_path / "state",
                enable_repair=True,
                approval_ttl=1 if case == "expiry" else 90,
            ),
            base_url="http://127.0.0.1",
        ) as client:
            accepted = client.post("/api/v1/runs", json=body, headers=headers)
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()["run_id"]
            path = f"/api/v1/runs/{run_id}"
            a = wait_approval(client, ta, path)
            assert a["path"] == "calc.py" and a["before_sha256"]
            assert BAD in a["diff"] and WRONG in a["diff"]
            assert source.read_text() == broken
            endpoint = f"{path}/approvals/" + a["id"]
            assert (
                client.post(endpoint, json={"decision": "approve"}, headers=auth(tb)).status_code
                == 404
            )
            assert client.get(path + "/report", headers=auth(tb)).status_code == 404
            assert client.post("/api/v1/runs", json=body, headers=headers).json() == accepted.json()
            if case == "expiry":
                time.sleep(1.1)
                assert (
                    client.post(endpoint, json={"decision": "approve"}, headers=headers).status_code
                    == 409
                )
            elif case == "cancel":
                assert client.post(path + "/cancel", headers=headers).status_code == 204
                assert (
                    client.post(endpoint, json={"decision": "approve"}, headers=headers).status_code
                    == 409
                )
            else:
                if case == "hash_conflict":
                    source.write_text(broken + "# external edit\n", encoding="utf-8")
                decision = {"decision": "deny" if case == "deny" else "approve"}
                assert client.post(endpoint, json=decision, headers=headers).status_code == 204
                assert client.post(endpoint, json=decision, headers=headers).status_code == 409
            if case in {"success", "stale", "test_cancel"}:
                for tool_name in (
                    ["run_command"]
                    if case == "test_cancel"
                    else ["run_command", "edit_file", "run_command"]
                ):
                    a = wait_approval(client, ta, path)
                    assert a["tool"] == tool_name
                    if tool_name == "run_command":
                        assert a["cwd"] == "." and a["argv"][1:] == ["-B", "-m", "unittest", "-q"]
                    assert (
                        client.post(
                            f"{path}/approvals/{a['id']}",
                            json={"decision": "approve"},
                            headers=headers,
                        ).status_code
                        == 204
                    )
                if case == "test_cancel":
                    deadline = time.monotonic() + 8
                    while not (root / ".test-started").exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    assert (root / ".test-started").exists()
                    assert client.post(path + "/cancel", headers=headers).status_code == 204
            done = wait_done(client, ta, run_id)
            report = client.get(path + "/report", headers=headers).json()
            stream = client.get(path + "/events", headers=headers).text
            if case in {"success", "stale"}:
                assert done["status"] == "completed"
                assert report["verification"] == ("passed" if case == "success" else "stale")
                assert [c["exit_code"] for c in report["checks"]] == [1, 0]
                assert [c["observed"] for c in report["checks"]] == ["failed", "passed"]
                assert report["checks"][0]["state"] == "stale"
                assert len(report["patches"]) == 2
                assert report["changed_paths"] == (
                    ["calc.py"] if case == "success" else ["calc.py", "late-change.txt"]
                )
                assert source.read_text() == broken.replace(BAD, FIXED)
            elif case == "test_cancel":
                assert done["status"] == "cancelled" and report["verification"] == "cancelled"
                assert report["checks"][0]["cleanup_confirmed"] is True
                assert not (root / ".test-finished").exists()
            else:
                expected = broken + ("# external edit\n" if case == "hash_conflict" else "")
                assert source.read_text() == expected
                assert report["patches"] == [] and report["verification"] == "not_run"
                if case == "hash_conflict":
                    assert "FILE_CHANGED" in stream
                if case == "expiry":
                    assert "approval.expired" in stream
                if case == "cancel":
                    assert done["status"] == "cancelled"
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_real_run_cursor_restart_and_tenant_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Set STELLARWEAVER_TEST_DATABASE_DSN to a migrated test database")
    engine = engine_for(dsn)
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "test-visible-file.txt").write_text("fixture")
    monkeypatch.setattr(
        "stellarweaver.web.execution.offline_repository_model",
        lambda: FakeModel(
            [
                AssistantReply(
                    tool_calls=(
                        ToolCall("git-status", "git_status", "{}"),
                        ToolCall("list-files", "list_files", "{}"),
                    ),
                    stop_reason="tool_calls",
                ),
                AssistantReply("Read the actual workspace"),
            ]
        ),
    )
    a, b = uuid4(), uuid4()
    ta, tb = uuid4().hex, uuid4().hex
    principals = {ta: Principal(a, (root,)), tb: Principal(b, (root,))}
    path = "/api/v1/runs"
    body = {
        "workspace_id": next(iter(principals[ta].workspace_map())),
        "request_id": str(uuid4()),
        "prompt": "Read the actual Git checkout",
    }
    try:
        with TestClient(
            create_app(engine, principals, tmp_path / "state"), base_url="http://127.0.0.1"
        ) as client:
            assert client.get("/api/v1/workspaces").status_code == 401
            assert (
                client.get(
                    "/api/v1/workspaces", headers={**auth(ta), "Host": "example.invalid"}
                ).status_code
                == 400
            )
            assert client.post(path, json=body, headers=auth(tb)).status_code == 404
            assert (
                client.post(path, json={**body, "mode": "repair"}, headers=auth(ta)).status_code
                == 403
            )
            accepted = client.post(path, json=body, headers=auth(ta))
            assert accepted.status_code == 202, accepted.text
            ids = accepted.json()
            assert client.post(path, json=body, headers=auth(ta)).json() == ids
            assert (
                client.post(
                    path, json={**body, "prompt": "different"}, headers=auth(ta)
                ).status_code
                == 409
            )
            run_id = ids["run_id"]
            done = wait_done(client, ta, run_id)
            assert done["status"] == "completed"
            assert done["outcome"]["tool_calls"] == 2
            for endpoint in (f"{path}/{run_id}", f"{path}/{run_id}/events"):
                assert client.get(endpoint, headers=auth(tb)).status_code == 404
            assert client.get(path, headers=auth(tb)).json() == []
            stream = client.get(f"{path}/{run_id}/events", headers=auth(ta)).text
            assert "test-visible-file.txt" in stream
            assert str(root).replace("\\", "\\\\") not in stream
            events = [
                json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ")
            ]
            assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
            assert events[-1]["type"] == "run.completed"
            tail = client.get(
                f"{path}/{run_id}/events",
                headers={**auth(ta), "Last-Event-ID": str(events[-2]["seq"])},
            )
            assert [
                json.loads(line[6:]) for line in tail.text.splitlines() if line.startswith("data: ")
            ] == events[-1:]
            with scoped(engine, str(a)) as connection:
                operations = connection.execute(
                    text("SELECT tool_name,state,result FROM sw.operations WHERE run_id=:id"),
                    {"id": run_id},
                ).all()
                assert {row.tool_name: row.state for row in operations} == {
                    "git_status": "ok",
                    "list_files": "ok",
                }
                git_result = next(row.result for row in operations if row.tool_name == "git_status")
                assert git_result["data"]["untracked"] == ["test-visible-file.txt"]
                assert git_result["data"]["stdout"].endswith("␀")
                roles = (
                    connection.execute(
                        text(
                            "SELECT body->>'role' FROM sw.messages WHERE run_id=:id ORDER BY position"
                        ),
                        {"id": run_id},
                    )
                    .scalars()
                    .all()
                )
                assert roles == ["system", "user", "assistant", "tool", "tool", "assistant"]
            with engine.begin() as connection:
                assert connection.execute(text("SELECT count(*) FROM sw.runs")).scalar_one() == 0
            with pytest.raises(DBAPIError):
                with scoped(engine, str(b)) as connection:
                    connection.execute(
                        text("INSERT INTO sw.tenants VALUES (:id)"), {"id": str(uuid4())}
                    )
            # Simulate an accepted run whose process exited before publishing a result.
            unfinished, _ = submit(
                engine, str(a), body["workspace_id"], str(uuid4()), "Unfinished", "fake"
            )
        with TestClient(
            create_app(engine, principals, tmp_path / "state"), base_url="http://127.0.0.1"
        ) as restarted:
            assert restarted.get(f"{path}/{run_id}", headers=auth(ta)).json() == done
            assert restarted.get(f"{path}/{run_id}/events", headers=auth(ta)).text == stream
            old = unfinished["run_id"]
            assert (
                restarted.get(f"{path}/{old}", headers=auth(ta)).json()["status"] == "interrupted"
            )
            interrupted = restarted.get(f"{path}/{old}/events", headers=auth(ta)).text
            assert "run.interrupted" in interrupted
            assert "tool.started" not in interrupted
    finally:
        engine.dispose()
