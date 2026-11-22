"""Git, PostgreSQL, and Docker acceptance tests for isolated Web tasks."""

import asyncio
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient  # noqa: E402

from stellarweaver.domain import AssistantReply, Message, ModelStreamEvent, ToolCall  # noqa: E402
from stellarweaver.providers.fake import FakeModel  # noqa: E402
from stellarweaver.tools.workspace import ToolError  # noqa: E402
from stellarweaver.web.app import Principal, create_app  # noqa: E402
from stellarweaver.web.db import claim, engine_for, event_page  # noqa: E402
from stellarweaver.web.isolation import DockerRunner, Isolation, git, task_worktree  # noqa: E402
from stellarweaver.web.journal import PgJournal  # noqa: E402
from stellarweaver.web.review import apply_selection, candidate, selected_patch  # noqa: E402
from stellarweaver.web.worker import run_worker  # noqa: E402

UNITTEST = ("python", "-m", "unittest", "-q")
BUILD = ("python", "build_check.py")
SLEEP = ("python", "-c", "import time; time.sleep(30)")
BASE = "TASK = 'base'\n" + "\n" * 14 + "SECOND = 'base'\n"
PROBE = """
import os
import unittest
from pathlib import Path
import calc

class TaskTests(unittest.TestCase):
    def test_workspace_and_test_environment(self):
        expected = {"a": {"a.txt"}, "b": {"b.txt"}, "base": set()}[calc.TASK]
        self.assertEqual({p.name for p in Path.cwd().glob("?.txt")}, expected)
        self.assertFalse(Path(".env").exists())
        self.assertFalse(Path(".git").exists())
        self.assertFalse(Path("/state").exists())
        self.assertFalse(Path("/workspaces/project").exists())
        self.assertFalse(Path("/var/run/docker.sock").exists())
        self.assertEqual(os.listdir("/sys/class/net"), ["lo"])
        self.assertNotEqual(os.getuid(), 0)
        for key in ("STELLARWEAVER_API_KEY", "STELLARWEAVER_DATABASE_DSN", "SW_WEB_DB_PASSWORD"):
            self.assertNotIn(key, os.environ)
        Path("discarded.txt").write_text("inside the container")
"""


def repository(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / "source"
    root.mkdir()
    (root / "calc.py").write_bytes(BASE.encode())
    (root / "test_calc.py").write_bytes(PROBE.encode())
    (root / "build_check.py").write_text("print('build validated')\n", encoding="utf-8")
    git(root, "init", "--template=")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Fixture",
    )
    (root / ".env").write_text("PRIVATE_SENTINEL=source-owned\n", encoding="utf-8")
    return root


def test_worktrees_export_and_apply_only_selected_hunks(tmp_path: Path) -> None:
    source = repository(tmp_path)
    tenant = str(uuid4())
    state = tmp_path.resolve() / "state"
    first, second = (task_worktree(state, tenant, str(uuid4()), source) for _ in range(2))
    modified = BASE.replace("TASK = 'base'", "TASK = 'a'").replace(
        "SECOND = 'base'", "SECOND = 'other'"
    )
    (first / "calc.py").write_bytes(modified.encode())
    (first / "a.txt").write_bytes(b"one\n")
    assert (second / "calc.py").read_text() == BASE
    assert not (second / "a.txt").exists()
    assert not (first / ".env").exists()
    result = candidate(first)
    calc = next(file for file in result["files"] if file["path"] == "calc.py")
    assert len(calc["hunks"]) == 2
    chosen = calc["hunks"][1]["id"]
    patch = selected_patch(result, [chosen])
    apply_selection(second, result["base"], patch)
    assert (second / "calc.py").read_text() == BASE.replace("SECOND = 'base'", "SECOND = 'other'")
    with pytest.raises(ToolError, match="cleanly"):
        apply_selection(second, result["base"], patch)
    with pytest.raises(ToolError, match="valid, unique"):
        selected_patch(result, [chosen, chosen])
    assert (source / "calc.py").read_text() == BASE


def test_committed_credentials_are_refused(tmp_path: Path) -> None:
    source = repository(tmp_path)
    git(source, "add", "--", ".env")
    git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Protected fixture",
    )
    with pytest.raises(ToolError, match="protected file"):
        task_worktree(tmp_path.resolve() / "state", str(uuid4()), str(uuid4()), source)


async def test_docker_runner_exit_code_timeout_and_cleanup(tmp_path: Path) -> None:
    image = os.getenv("STELLARWEAVER_G4_TEST_IMAGE")
    if not image:
        pytest.skip("Set STELLARWEAVER_G4_TEST_IMAGE to a local Linux Python runner image")
    source = await asyncio.to_thread(repository, tmp_path)
    work = await asyncio.to_thread(
        task_worktree, source.parent / "state", str(uuid4()), str(uuid4()), source
    )
    runner = DockerRunner(work, Isolation(image, (UNITTEST, SLEEP), f"test-{uuid4().hex}"))
    success = await runner(list(UNITTEST), work)
    assert success["status"] == "ok", success
    assert "Ran 1 test" in success["data"]["stderr"]
    (work / "calc.py").write_text("TASK = 'invalid'\n", encoding="utf-8")
    failure = await runner(list(UNITTEST), work)
    assert failure["status"] == "error" and failure["data"]["exit_code"] == 1, failure
    timed_out = await runner(list(SLEEP), work, timeout_seconds=0.4)
    assert timed_out["data"]["code"] == "COMMAND_TIMEOUT", timed_out
    assert timed_out["data"]["process_management"]["cleanup_confirmed"]
    assert not (work / "discarded.txt").exists()

    def containers(*, running: bool = False) -> bytes:
        return subprocess.check_output(
            [
                "docker",
                "ps",
                "-q" if running else "-aq",
                "--filter",
                f"label=io.stellarweaver.runner={runner.isolation.namespace}",
            ],
            timeout=5,
        )

    running = asyncio.create_task(runner(list(SLEEP), work))
    try:
        deadline = time.monotonic() + 8
        while not await asyncio.to_thread(containers, running=True):
            assert time.monotonic() < deadline, "The cancellation fixture did not start"
            await asyncio.sleep(0.1)
    finally:
        running.cancel()
        cancelled = await running
    assert cancelled["status"] == "cancelled"
    assert cancelled["data"]["process_management"]["cleanup_confirmed"]
    assert not await asyncio.to_thread(containers)


def tool(index: int, name: str, **arguments: str | list[str]) -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall(f"fixture-{index}", name, json.dumps(arguments)),),
        stop_reason="tool_calls",
    )


@pytest.mark.postgres
def test_two_web_tasks_followup_recovery_and_partial_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    image = os.getenv("STELLARWEAVER_G4_TEST_IMAGE")
    if not dsn or not image:
        pytest.skip(
            "Requires an isolated PostgreSQL database and a local Linux Python runner image"
        )
    source = repository(tmp_path)
    state = tmp_path.resolve() / "state"
    principal = Principal(uuid4(), (source,))
    tenant = str(principal.tenant)
    roots = principal.workspace_map()
    namespace = f"test-{uuid4().hex}"
    isolation = Isolation(image, (UNITTEST, BUILD), namespace)
    token, other_token = uuid4().hex, uuid4().hex
    headers = {"Authorization": f"Bearer {token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    engine = engine_for(dsn)
    monkeypatch.setenv("STELLARWEAVER_API_KEY", "test-provider-credential")
    monkeypatch.setenv("SW_WEB_DB_PASSWORD", "test-db-credential")
    release_stream = Event()

    def repair(_commands: tuple[tuple[str, ...], ...]) -> FakeModel:
        phase = 0

        def respond(request):
            nonlocal phase
            task = next(msg.content for msg in request.messages if msg.role == "user")
            role = "a" if "task-a" in task else "b"
            phase += 1
            last = json.loads(request.messages[-1].content)["data"] if phase in {2, 4} else {}
            if phase == 1 or (phase == 3 and role == "a"):
                return tool(phase, "read_file", path="calc.py")
            if phase == 2:
                return tool(
                    phase,
                    "edit_file",
                    path="calc.py",
                    old_text="TASK = 'base'",
                    new_text=f"TASK = '{role}'",
                    expected_sha256=last["sha256"],
                )
            if phase == 4 and role == "a":
                return tool(
                    phase,
                    "edit_file",
                    path="calc.py",
                    old_text="SECOND = 'base'",
                    new_text="SECOND = 'reviewed'",
                    expected_sha256=last["sha256"],
                )
            if phase == (5 if role == "a" else 3):
                return tool(phase, "create_file", path=f"{role}.txt", content=f"task-{role}\n")
            if phase == (6 if role == "a" else 4):
                return tool(phase, "run_command", argv=list(UNITTEST))
            if phase == (7 if role == "a" else 5):
                return tool(phase, "run_command", argv=list(BUILD))
            return AssistantReply(f"final answer from task-{role}")

        return FakeModel(respond)

    def followup() -> FakeModel:
        def respond(request):
            users = [msg.content for msg in request.messages if msg.role == "user"]
            if "resume recovery" in users[-1]:
                assert "edit_file" in users[-1] and "uncertain.txt" in users[-1]
                assert len(users) == 1
                return AssistantReply("Recovered by inspection")
            assert users[0] == "task-a"
            assert any(msg.content == "final answer from task-a" for msg in request.messages)
            assert all(not msg.tool_calls for msg in request.messages)
            return AssistantReply("Followed prior answer")

        class StreamingFake(FakeModel):
            async def stream(self, request):
                reply = await self.complete(request)
                if reply.text == "Followed prior answer":
                    yield ModelStreamEvent("text.delta", text="Followed ")
                    assert await asyncio.to_thread(release_stream.wait, 10)
                    yield ModelStreamEvent("text.delta", text="prior answer")
                else:
                    yield ModelStreamEvent("text.delta", text=reply.text)
                yield ModelStreamEvent("response.completed", reply=reply)

        return StreamingFake(respond)

    monkeypatch.setattr("stellarweaver.web.execution.offline_repair_model", repair)
    monkeypatch.setattr("stellarweaver.web.execution.offline_repository_model", followup)
    api = create_app(
        engine,
        {token: principal, other_token: Principal(uuid4(), (source,))},
        state,
        queued=True,
        enable_repair=True,
        isolated=True,
    )
    try:
        with (
            TestClient(api, base_url="http://127.0.0.1") as client,
            ThreadPoolExecutor() as workers,
        ):
            assert client.get("/api/v1/workspaces", headers=headers).json()["isolated"]

            def submit(prompt: str, **args):
                return client.post(
                    "/api/v1/runs",
                    headers=headers,
                    json={
                        "workspace_id": next(iter(roots)),
                        "request_id": str(uuid4()),
                        "prompt": prompt,
                        **args,
                    },
                )

            def work() -> None:
                run_worker(
                    engine,
                    tenant,
                    roots,
                    state,
                    provider="fake",
                    enable_repair=True,
                    isolation=isolation,
                    once=True,
                )

            def inspect(run_id: str) -> dict:
                return client.get(f"/api/v1/runs/{run_id}", headers=headers).json()

            def report(run_id: str) -> dict:
                response = client.get(f"/api/v1/runs/{run_id}/report", headers=headers)
                assert response.is_success, response.text
                return response.json()

            a = submit("task-a", mode="repair").json()
            b = submit("task-b", mode="repair").json()
            assert a["session_id"] != b["session_id"]
            assert submit("too early", session_id=a["session_id"]).status_code == 409
            for identifier, count in ((a["run_id"], 5), (b["run_id"], 4)):
                future = workers.submit(work)
                approved: set[str] = set()
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    row = inspect(identifier)
                    if row["status"] != "active":
                        break
                    pending = row.get("pending_approval")
                    if pending and pending["id"] not in approved:
                        reply = client.post(
                            f"/api/v1/runs/{identifier}/approvals/{pending['id']}",
                            headers=headers,
                            json={"decision": "approve"},
                        )
                        assert reply.status_code == 204, reply.text
                        approved.add(pending["id"])
                    time.sleep(0.04)
                assert row["status"] == "completed", row.get("outcome")
                future.result(timeout=3)
                assert len(approved) == count
                assert report(identifier)["verification"] == "passed", report(identifier)

            rep_a = report(a["run_id"])
            rep_b = report(b["run_id"])
            assert {f["path"] for f in rep_a["review"]["files"]} == {"calc.py", "a.txt"}
            assert {f["path"] for f in rep_b["review"]["files"]} == {"calc.py", "b.txt"}
            assert not (source / "a.txt").exists() and (source / "calc.py").read_text() == BASE
            calc = next(f for f in rep_a["review"]["files"] if f["path"] == "calc.py")
            assert len(calc["hunks"]) == 2
            endpoint = f"/api/v1/runs/{a['run_id']}/review"
            assert (
                client.post(
                    endpoint,
                    headers=other_headers,
                    json={
                        "request_id": str(uuid4()),
                        "hunks": [calc["hunks"][1]["id"]],
                    },
                ).status_code
                == 404
            )
            response = client.post(
                endpoint,
                headers=headers,
                json={
                    "request_id": str(uuid4()),
                    "hunks": [calc["hunks"][1]["id"]],
                },
            )
            assert response.status_code == 202, response.text
            verification = response.json()
            work()
            verified = report(verification["run_id"])
            assert verified["verification"] == "passed"
            assert verified["changed_paths"] == ["calc.py"]
            assert [check["argv"] for check in verified["checks"]] == [list(UNITTEST), list(BUILD)]
            assert "build validated" in verified["checks"][1]["stdout"]

            c = submit("read previous answer", session_id=a["session_id"]).json()
            streaming = workers.submit(work)
            try:
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    partial = event_page(engine, tenant, c["run_id"], 0)
                    if any(item["type"] == "assistant.delta" for item in partial):
                        break
                    time.sleep(0.05)
                assert partial[-1]["payload"]["content"] == "Followed "
                assert inspect(c["run_id"])["status"] == "active"
            finally:
                release_stream.set()
            streaming.result(timeout=10)
            assert inspect(c["run_id"])["status"] == "completed"
            events = client.get(f"/api/v1/runs/{c['run_id']}/events", headers=headers).text
            assert events.index("assistant.delta") < events.index("assistant.completed")
            assert "Followed prior answer" in events
            payloads = {
                event["type"]: event["payload"]
                for line in events.splitlines()
                if line.startswith("data: ")
                for event in [json.loads(line[6:])]
            }
            assert payloads["model.started"]["context_window_tokens"] == 200_000
            assert payloads["model.started"]["input_estimated"] is True
            assert payloads["model.completed"]["input_tokens"] > 0
            assert payloads["assistant.completed"]["has_tools"] is False
            turns = client.get(f"/api/v1/runs?session_id={a['session_id']}", headers=headers).json()
            assert [item["run_id"] for item in turns] == [c["run_id"], a["run_id"]]
            assert turns[0]["final_text"] == "Followed prior answer"
            threads = client.get("/api/v1/threads", headers=headers).json()
            thread = [item for item in threads if item["session_id"] == a["session_id"]]
            assert len(thread) == 1 and thread[0]["run_id"] == c["run_id"]
            assert thread[0]["title"] == "task-a"
            assert client.get("/api/v1/threads", headers=other_headers).json() == []

            interrupted = submit(
                "interrupted edit", mode="repair", session_id=a["session_id"]
            ).json()
            owner = str(uuid4())
            attempt = claim(engine, tenant, owner)
            assert attempt is not None and attempt["run_id"] == interrupted["run_id"]
            journal = PgJournal(
                engine,
                tenant,
                a["session_id"],
                interrupted["run_id"],
                source,
                state,
                claim=(owner, attempt["attempt"]),
            )
            call = ToolCall("uncertain", "edit_file", '{"path":"uncertain.txt"}')
            assistant = journal.record_assistant(Message("assistant", tool_calls=(call,)))
            operation = journal.operation_id_for(assistant, call.call_id)
            intent = {
                "operation_id": operation,
                "call_id": call.call_id,
                "tool": call.name,
                "path": "uncertain.txt",
            }
            journal.record_approved(assistant, {**intent, "decision": "allow"})
            journal.record_intent(assistant, intent)
            work()
            interrupted_run = inspect(interrupted["run_id"])
            assert interrupted_run["outcome"]["unfinished_tools"] == 1
            assert interrupted_run["unconfirmed_operations"][0]["path"] == "uncertain.txt"
            assert submit("resume recovery", session_id=a["session_id"]).status_code == 409
            resumed = submit(
                "resume recovery",
                session_id=a["session_id"],
                continuation="fresh",
                acknowledge_interrupted=True,
            ).json()
            work()
            assert inspect(resumed["run_id"])["status"] == "completed"
            recovered = client.get(f"/api/v1/runs/{resumed['run_id']}/events", headers=headers).text
            assert "uncertain.txt" in recovered and "Recovered by inspection" in recovered
            assert "tool.started" not in recovered
    finally:
        engine.dispose()
