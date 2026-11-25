"""Offline acceptance fixtures for the first N-stage batch."""

import json
import os
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from stellarweaver.changes import ChangesManager
from stellarweaver.config import AppConfig, ContextConfig, HarnessConfig, PolicyConfig
from stellarweaver.context.manager import ContextManager
from stellarweaver.domain import AssistantReply, JSONDict, Message, ToolCall, ToolResult
from stellarweaver.evals.provenance import canonical_json
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import attach_harness, build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.web import isolation
from stellarweaver.web.isolation import DockerRunner, Isolation, git
from stellarweaver.web.remote_runner import RemoteRunner


def call(name: str, args: JSONDict, number: int = 1) -> ToolCall:
    return ToolCall(str(number), name, json.dumps(args))


def edit(args: JSONDict, number: int = 1) -> AssistantReply:
    return AssistantReply(tool_calls=(call("edit_file", args, number),), stop_reason="tool_calls")


def repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "--template=")
    return root


async def test_multi_edit_original_bytes_single_replace_and_undo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = "# 测试\r\na = 1\r\n\r\nb = 2\r\n\r\nc = 3".encode()
    edits = [
        {"old_text": "a = 1", "new_text": "a = 10"},
        {"old_text": "b = 2", "new_text": "b = 20"},
        {"old_text": "c = 3", "new_text": "c = 30"},
    ]
    expected = original
    legacy = []
    for index, item in enumerate(edits):
        legacy.append(
            edit(
                {
                    "path": "sample.py",
                    "expected_sha256": sha256(expected).hexdigest(),
                    **item,
                },
                index + 1,
            )
        )
        expected = expected.replace(item["old_text"].encode(), item["new_text"].encode(), 1)
    multi = edit(
        {
            "path": "sample.py",
            "expected_sha256": sha256(original).hexdigest(),
            "edits": list(reversed(edits)),
        }
    )
    measurements = {}
    original_replace = os.replace
    for enabled, responses in ((False, legacy), (True, [multi])):
        root = repo(tmp_path / f"repo-{enabled}")
        target = root / "sample.py"
        target.write_bytes(original)
        writes = []

        def count_replace(source, destination, *, current_target=target, captured_writes=writes):
            if Path(destination) == current_target:
                captured_writes.append(destination)
            original_replace(source, destination)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", count_replace)
            with SessionStore(root, tmp_path / f"state-{enabled}") as store:
                config = AppConfig(
                    policy=PolicyConfig(preset="workspace-edit"),
                    harness=HarnessConfig(multi_edit_tools=enabled),
                )
                agent = build_agent(config, FakeModel([*responses, AssistantReply("Done")]), store)
                if not enabled:
                    assert sha256(canonical_json(agent.registry.schemas())).hexdigest() == (
                        "71dcf99f3d0129444147c04f1c44df7bc239619ad222b255de6b3647aec074b6"
                    )
                outcome = await agent.run("Modify the three assignments")
                count = 1 if enabled else 3
                assert target.read_bytes() == expected
                assert outcome.tool_calls == len(writes) == count
                assert len(tuple(store.artifacts_dir.glob("*.before"))) == count
                assert sum(result.data["replacement_count"] for result in agent.results) == 3
                if enabled:
                    artifact = agent.results[0].data["diff_artifact_id"]
                    git(
                        root, "-c", "core.autocrlf=false", "apply", "--check", "--reverse", artifact
                    )
                measurements[str(enabled)] = {
                    "tool_calls": count,
                    "model_calls": outcome.model_calls,
                    "replacements": len(writes),
                    "backups": count,
                    "tool_ms": sum(result.duration_ms for result in agent.results),
                }
                manager = ChangesManager(root, store, agent.registry.policy)
                for _ in range(count):
                    assert (await manager.undo())["status"] == "ok"
                assert target.read_bytes() == original
    print(f"edit fixture: {measurements}")


@pytest.mark.parametrize(
    ("edits", "code"),
    [
        (
            [{"old_text": "one two", "new_text": "A"}, {"old_text": "two three", "new_text": "B"}],
            "EDIT_OVERLAPPING",
        ),
        (
            [{"old_text": "one", "new_text": "absent"}, {"old_text": "absent", "new_text": "B"}],
            "EDIT_NOT_FOUND",
        ),
        (
            [{"old_text": "one", "new_text": "A"}, {"old_text": "dup", "new_text": "B"}],
            "EDIT_AMBIGUOUS",
        ),
    ],
)
async def test_multi_edit_rejects_whole_batch(
    tmp_path: Path, edits: list[JSONDict], code: str
) -> None:
    target = tmp_path / "file.txt"
    original = b"one two three dup dup\n"
    target.write_bytes(original)
    registry = ToolRegistry(
        tmp_path,
        PolicyEngine("workspace-edit", workspace=tmp_path),
        harness=HarnessConfig(multi_edit_tools=True),
    )
    result = await registry.dispatch(
        call(
            "edit_file",
            {
                "path": target.name,
                "expected_sha256": sha256(original).hexdigest(),
                "edits": edits,
            },
        )
    )
    assert result.status == "error" and result.data["code"] == code
    assert target.read_bytes() == original
    assert not registry.artifacts_dir.exists()


async def test_syntax_diagnostic_reaches_next_model_and_metrics_use_real_usage(
    tmp_path: Path,
) -> None:
    root = repo(tmp_path / "sandbox")
    target = root / "sample.py"
    original = b"value = 1\n"
    target.write_bytes(original)

    def visitor(request):
        tool = next((item for item in request.messages if item.role == "tool"), None)
        if tool is None:
            return edit(
                {
                    "path": target.name,
                    "expected_sha256": sha256(original).hexdigest(),
                    "old_text": "1",
                    "new_text": "(",
                }
            )
        observed = json.loads(tool.content)
        assert observed["status"] == "ok"
        assert target.read_bytes() == b"value = (\n"
        diagnostic = observed["data"]["syntax_diagnostic"]
        assert diagnostic["code"] == "PYTHON_SYNTAX_ERROR" and diagnostic["line"] == 1
        return AssistantReply("Saw the error.", usage={"input_tokens": 6000, "output_tokens": 10})

    with SessionStore(root, tmp_path / "state") as store:
        config = AppConfig(
            policy=PolicyConfig(preset="workspace-edit"),
            harness=HarnessConfig(post_edit_syntax_hook=True, pipeline_metrics=True),
        )
        agent = build_agent(config, FakeModel(visitor), store)
        assert sha256(canonical_json(agent.registry.schemas())).hexdigest() == (
            "71dcf99f3d0129444147c04f1c44df7bc239619ad222b255de6b3647aec074b6"
        )
        outcome = await agent.run("Edit and inspect the syntax diagnostic")
        assert outcome.run_status == "completed"
        estimated, measured = [
            event.payload for event in store.load_events() if event.type == "model.completed"
        ]
        assert "input_estimate_error_ratio" not in estimated and estimated["input_estimated"]
        assert measured["input_estimate_error_ratio"] == (measured["input_estimate"] - 6000) / 6000
        print(
            f"token fixture: estimate={measured['input_estimate']} actual={measured['input_tokens']} ratio={measured['input_estimate_error_ratio']:.4f}"
        )


def test_projected_long_test_output_keeps_summary_and_assertion() -> None:
    stdout = "正常输出甲" * 1000 + "\nFAILED tests/test_sample.py::test_value"
    stderr = "诊断输出乙" * 1000 + "\nAssertionError: expected 2, got 1"
    test_call = call("run_tests", {})
    tool_result = ToolResult(
        test_call.call_id,
        "operation",
        "run_tests",
        "error",
        f"stdout:\n{stdout}\nstderr:\n{stderr}",
        {"stdout": stdout, "stderr": stderr, "exit_code": 1},
    )
    tool = Message("tool", json.dumps(asdict(tool_result), ensure_ascii=False), tool_call_id="1")
    history = (Message("user", "Run tests"), Message("assistant", tool_calls=(test_call,)), tool)
    output = {}
    for smart in (False, True):
        projection = ContextManager(smart_tool_truncation=smart).build(
            history,
            (),
            100,
            4000,
            ContextConfig(reserve_tokens=0),
        )
        projected = next(message for message in projection.messages if message.role == "tool")
        result = json.loads(projected.content)
        content = result["content"]
        assert projected.tool_call_id == "1" and result["data"]["exit_code"] == 1
        matches = [
            marker in content
            for marker in ("FAILED tests/test_sample.py", "AssertionError: expected 2")
        ]
        assert matches == [smart, smart]
        if smart:
            assert len(content.encode("utf-8")) <= 1024 and "\ufffd" not in content
        output[str(smart)] = {"preserved_markers": sum(matches), "bytes": len(content.encode())}
    assert json.loads(tool.content)["data"]["stdout"] == stdout
    print(f"log fixture: {output}")


async def test_navigation_prefilter_finds_symbol_at_end_of_500_files(tmp_path: Path) -> None:
    root = repo(tmp_path / "navigation")
    for number in range(500):
        body = "# 合成夹具\ndef fixture():\n    return None\n"
        if number == 499:
            body = "def tａrget_symbol():\n    return 1\n"
        (root / f"module_{number:03}.py").write_text(body, encoding="utf-8")
    output = {}
    for prefilter in (False, True):
        registry = ToolRegistry(root, PolicyEngine("read-only", workspace=root))
        attach_harness(
            registry,
            AppConfig(harness=HarnessConfig(issue_navigation=True, navigation_prefilter=prefilter)),
        )
        result = await registry.dispatch(call("find_symbol", {"symbol": "target_symbol"}))
        assert result.status == "ok" and result.data["truncated"] is not prefilter
        matches = result.data["matches"]
        assert [item["path"] for item in matches] == (["module_499.py"] if prefilter else [])
        if prefilter:
            assert result.data["prefilter_scanned"] == 500
            assert result.data["indexed_files"] == 1
        output[str(prefilter)] = {
            "matches": len(matches),
            "indexed": result.data["indexed_files"],
            "query_ms": result.data["duration_ms"],
        }
    print(f"navigation fixture: {output}")


async def test_docker_pipeline_timing_separates_pack_and_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repo(tmp_path / "runner")
    (root / "sample.py").write_bytes(b"x = 1\n")
    image = "sha256:" + "b" * 64
    argv = ["python", "-m", "unittest"]

    async def fake_command(command, *args, **kwargs):
        stdout = json.dumps({"Id": image, "Config": {}}) if command[1] == "image" else ""
        return {"status": "ok", "data": {"exit_code": 0, "stdout": stdout}}

    monkeypatch.setattr(isolation, "execute_command", fake_command)
    for enabled in (False, True):
        runner = DockerRunner(
            root, Isolation(image, (tuple(argv),), "fixture"), pipeline_metrics=enabled
        )
        result = await runner(argv, root)
        timings = result["data"].get("sandbox_timing_ms")
        if enabled:
            assert set(timings) == {
                "pack",
                "archive_hash",
                "image_inspect",
                "container_create",
                "execute",
                "cleanup",
            }
            assert all(value >= 0 for value in timings.values())
            print(f"sandbox fixture (mocked Docker): {timings}")
        else:
            assert timings is None


async def test_remote_pipeline_timing_merges_worker_and_executor_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from stellarweaver.web.broker import create_broker

    root = repo(tmp_path / "remote")
    (root / "sample.py").write_bytes(b"x = 1\n")
    token = uuid4().hex
    argv = ["python", "-m", "unittest"]
    sandbox = Isolation("fixture:local", (tuple(argv),), "fixture")
    broker = create_broker(sandbox, token)
    actual_client = httpx.AsyncClient

    def local_transport(*args, **kwargs):
        return actual_client(*args, transport=httpx.ASGITransport(app=broker), **kwargs)

    async def fake_execute(runner, archive, command, **kwargs):
        assert command == argv and archive.tell() == 0
        data = {"process_management": {"cleanup_confirmed": True}}
        if runner.pipeline_metrics:
            data["sandbox_timing_ms"] = {"execute": 3.5}
        return {"status": "ok", "data": data}

    monkeypatch.setattr(httpx, "AsyncClient", local_transport)
    monkeypatch.setattr(DockerRunner, "run_archive", fake_execute)
    for enabled in (False, True):
        runner = RemoteRunner(
            root, sandbox, "http://executor.invalid", token, pipeline_metrics=enabled
        )
        result = await runner(argv, root)
        assert result["status"] == "ok"
        timings = result["data"].get("sandbox_timing_ms")
        if enabled:
            assert set(timings) == {"pack", "executor_round_trip", "execute"}
            assert timings["execute"] == 3.5
        else:
            assert timings is None
