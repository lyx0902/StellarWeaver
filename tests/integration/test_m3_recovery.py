"""Integration E: real side effects, abrupt process death, and safe resumed runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry

FAULT_CHILD = r"""
import asyncio, json, os, sys
from pathlib import Path
from stellarweaver.domain import AssistantReply, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
case = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
workspace = Path(case["workspace"])
store = SessionStore(workspace, Path(case["state_dir"]))
Path(case["pointer"]).write_text(str(store.path), encoding="utf-8")
original = store.record_result

def die_before_result(assistant_id, result):
    if result.tool_name == case["tool"] and result.status == "ok":
        os._exit(91)
    return original(assistant_id, result)

store.record_result = die_before_result
policy = PolicyEngine("workspace-edit", workspace=workspace, policy_file=Path(case["policy"]))
replies = [
    AssistantReply(tool_calls=(ToolCall("inspect-before", "read_file", json.dumps({"path": case["read_path"]})),), stop_reason="tool_calls"),
    AssistantReply(tool_calls=(ToolCall("crash-operation", case["tool"], json.dumps(case["args"])),), stop_reason="tool_calls"),
]
registry = ToolRegistry(workspace, policy, artifacts_dir=store.artifacts_dir)
asyncio.run(Agent(FakeModel(replies), registry, store=store).run("Repair the addition function and verify it."))
raise RuntimeError("Fault injection did not reach a successful tool result")
"""


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_repo(tmp_path: Path, *, with_calc: bool = True) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "中文 interrupted repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, check=True)
    if with_calc:
        (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n\nclass TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )
    (root / "user-existing.txt").write_bytes(b"user changes must remain\r\n")
    protected = {name: sha(root / name) for name in ("test_calc.py", "user-existing.txt")}
    return root, tmp_path / "state", protected


def policy_file(tmp_path: Path, root: Path, argv: list[str]) -> Path:
    path = tmp_path / "scoped-policy.json"
    path.write_text(
        json.dumps(
            {
                "workspace": str(root.resolve()),
                "grants": [{"tool": "run_command", "argv": argv, "cwd": "."}],
            }
        ),
        encoding="utf-8",
    )
    return path


def crash_after_success(
    tmp_path: Path,
    root: Path,
    state: Path,
    policy: Path,
    tool: str,
    args: dict[str, Any],
    *,
    read_path: str = "calc.py",
) -> Path:
    pointer = tmp_path / "session-path.txt"
    case_file = tmp_path / "fault-case.json"
    case_file.write_text(
        json.dumps(
            {
                "workspace": str(root),
                "state_dir": str(state),
                "policy": str(policy),
                "pointer": str(pointer),
                "tool": tool,
                "args": args,
                "read_path": read_path,
            }
        ),
        encoding="utf-8",
    )
    child = subprocess.run(
        [sys.executable, "-c", FAULT_CHILD, str(case_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 91, child.stdout + child.stderr
    return Path(pointer.read_text(encoding="utf-8"))


def request_tool(name: str, arguments: dict[str, Any], identifier: str) -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall(identifier, name, json.dumps(arguments)),), stop_reason="tool_calls"
    )


@pytest.mark.parametrize("tool", ["edit_file", "create_file"])
async def test_real_file_write_crash_reconciles_once_then_resumes_tests(
    tmp_path: Path, tool: str
) -> None:
    root, state, protected = fixture_repo(tmp_path, with_calc=tool == "edit_file")
    command = [sys.executable, "-m", "unittest", "-q"]
    grants = policy_file(tmp_path, root, command)
    if tool == "edit_file":
        args: dict[str, Any] = {
            "path": "calc.py",
            "expected_sha256": sha(root / "calc.py"),
            "old_text": "return a - b",
            "new_text": "return a + b",
        }
    else:
        args = {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}
    database = crash_after_success(
        tmp_path, root, state, grants, tool, args, read_path="test_calc.py"
    )
    snapshot = SessionStore.inspect(database)
    pending = [entry for entry in snapshot.tool_executions if entry["tool_name"] == tool]
    assert len(pending) == 1 and pending[0]["state"] == "started"
    assert pending[0]["result_json"] is None
    after = (root / "calc.py").read_bytes(), (root / "calc.py").stat().st_mtime_ns
    requests = 0

    def continue_script(request: ModelRequest) -> AssistantReply:
        nonlocal requests
        requests += 1
        assert any(
            message.role == "user" and "Repair the addition" in message.content
            for message in request.messages
        )
        recovered = [
            json.loads(message.content)
            for message in request.messages
            if message.role == "tool" and message.tool_call_id == "crash-operation"
        ]
        assert len(recovered) == 1 and recovered[0]["data"]["recovered_from_state"] is True
        if requests == 1:
            return request_tool("read_file", {"path": "calc.py"}, "read-after-resume")
        if requests == 2:
            return request_tool("run_command", {"argv": command}, "verify-after-resume")
        return AssistantReply("The recovered change was read and its tests passed.")

    model = FakeModel(callback=continue_script)
    with SessionStore.open(database, workspace=root) as store:
        report = store.reconcile()
        assert report["requires_attention"] is False
        assert report["operations"][0]["data"]["recovered_from_state"] is True
        assert ((root / "calc.py").read_bytes(), (root / "calc.py").stat().st_mtime_ns) == after
        registry = ToolRegistry(
            root,
            PolicyEngine(workspace=root, policy_file=grants),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(model, registry, store=store).resume()
        assert outcome.run_status == "completed"
        assert outcome.verification_status == "passed"
        saved = store.load_results()
        assert len([result for result in saved if result.tool_name == tool]) == 1
        assert [
            result.data["exit_code"] for result in saved if result.tool_name == "run_command"
        ] == [0]
    assert requests == 3
    assert ((root / "calc.py").read_bytes(), (root / "calc.py").stat().st_mtime_ns) == after
    assert {name: sha(root / name) for name in protected} == protected
    independent = await asyncio.to_thread(
        subprocess.run, command, cwd=root, capture_output=True, timeout=15
    )
    assert independent.returncode == 0, independent.stderr.decode(errors="replace")


async def test_command_effect_before_crash_stays_unknown_and_never_reruns(tmp_path: Path) -> None:
    root, state, protected = fixture_repo(tmp_path)
    marker = root / "command-count.txt"
    command = [
        sys.executable,
        "-c",
        "from pathlib import Path; p=Path('command-count.txt'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')",
    ]
    grants = policy_file(tmp_path, root, command)
    database = crash_after_success(tmp_path, root, state, grants, "run_command", {"argv": command})
    assert marker.read_text() == "1"
    # Two independent resumes cannot turn a previously reconciled unknown into permission to retry.
    for _ in range(2):
        model = FakeModel([AssistantReply("Must never be called")])
        with SessionStore.open(database, workspace=root) as store:
            registry = ToolRegistry(
                root,
                PolicyEngine(workspace=root, policy_file=grants),
                artifacts_dir=store.artifacts_dir,
            )
            outcome = await Agent(model, registry, store=store).resume()
            assert outcome.run_status == "blocked"
            assert outcome.model_calls == 0 and not model.requests
            results = [
                result for result in store.load_results() if result.tool_name == "run_command"
            ]
            assert len(results) == 1 and results[0].status == "unknown"
        assert marker.read_text() == "1"
    assert {name: sha(root / name) for name in protected} == protected


async def test_user_edit_after_crash_is_preserved_and_blocks_automatic_resume(
    tmp_path: Path,
) -> None:
    root, state, protected = fixture_repo(tmp_path)
    command = [sys.executable, "-m", "unittest", "-q"]
    grants = policy_file(tmp_path, root, command)
    database = crash_after_success(
        tmp_path,
        root,
        state,
        grants,
        "edit_file",
        {
            "path": "calc.py",
            "expected_sha256": sha(root / "calc.py"),
            "old_text": "return a - b",
            "new_text": "return a + b",
        },
    )
    (root / "calc.py").write_bytes(b"# user changed it after the interruption\n")
    actual = sha(root / "calc.py")
    model = FakeModel([AssistantReply("Must never be called")])
    with SessionStore.open(database, workspace=root) as store:
        registry = ToolRegistry(
            root, PolicyEngine(workspace=root), artifacts_dir=store.artifacts_dir
        )
        outcome = await Agent(model, registry, store=store).resume()
        assert outcome.run_status == "blocked"
        assert not model.requests
        recovered = [result for result in store.load_results() if result.tool_name == "edit_file"]
        assert recovered[0].status == "unknown"
        assert recovered[0].data["code"] == "RECOVERY_FILE_CONFLICT"
    assert sha(root / "calc.py") == actual
    assert {name: sha(root / name) for name in protected} == protected
