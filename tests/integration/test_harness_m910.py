"""Exercise staged-only edits, evidence freshness and unchanged budget caps."""

import asyncio
import json
from pathlib import Path

from stellarweaver.config import AppConfig, HarnessConfig, LimitsConfig
from stellarweaver.domain import AssistantReply, JSONDict, ToolCall, ToolResult
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import build_agent
from stellarweaver.runtime.task_evidence import TaskEvidence
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.staging import register_staging
from stellarweaver.tools.workspace import Workspace, sha256_bytes
from stellarweaver.web.isolation import git


def repo(root: Path) -> Path:
    root.mkdir()
    (root / "a.txt").write_bytes(b"old line\r\n")
    (root / "old.txt").write_bytes(b"obsolete\n")
    git(root, "init", "--template=", "--quiet")
    git(root, "add", "--", "a.txt", "old.txt")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    return root


def stage(*changes: JSONDict) -> ToolCall:
    return ToolCall("stage", "stage_patch", json.dumps({"changes": changes}))


async def test_stage_patch_checks_all_files_and_never_applies_to_source(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    seen = []

    async def approve(invocation: JSONDict) -> str:
        seen.append(invocation)
        return "once"

    artifacts = tmp_path / "artifacts"
    registry = ToolRegistry(
        root,
        PolicyEngine("ask", workspace=root, interactive=True, approval=approve),
        artifacts_dir=artifacts,
    )
    register_staging(registry)
    result = await registry.dispatch(
        stage(
            {
                "path": "a.txt",
                "expected_sha256": sha256_bytes(b"old line\r\n"),
                "content": "new line\r\n",
            },
            {"path": "old.txt", "expected_sha256": sha256_bytes(b"obsolete\n"), "content": None},
            {"path": "docs/new.txt", "expected_sha256": None, "content": "new file\n"},
        )
    )
    assert result.status == "ok", result
    assert result.data["workspace_applied"] is False
    assert len(seen) == 1 and seen[0]["patch_sha256"] == result.data["patch_sha256"]
    artifact = Path(result.artifact_ids[0])
    assert sha256_bytes(await asyncio.to_thread(artifact.read_bytes)) == result.data["patch_sha256"]
    assert git(root, "status", "--porcelain") == b""
    assert (root / "a.txt").read_bytes() == b"old line\r\n"
    assert (root / "old.txt").exists() and not (root / "docs").exists()


async def test_stage_patch_rejects_a_changed_target_and_unsafe_paths(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")

    async def approve(_: JSONDict) -> str:
        (root / "a.txt").write_bytes(b"concurrent change\r\n")
        return "once"

    registry = ToolRegistry(
        root,
        PolicyEngine("ask", workspace=root, interactive=True, approval=approve),
        artifacts_dir=tmp_path / "artifacts",
    )
    register_staging(registry)
    unsafe = await registry.dispatch(
        stage({"path": ".env", "expected_sha256": None, "content": "synthetic only"})
    )
    assert unsafe.data["code"] == "PROTECTED_PATH"
    conflict = await registry.dispatch(
        stage(
            {
                "path": "a.txt",
                "expected_sha256": sha256_bytes(b"old line\r\n"),
                "content": "new line\r\n",
            },
        )
    )
    assert conflict.data["code"] == "PATCH_BASE_CHANGED"
    assert (root / "a.txt").read_bytes() == b"concurrent change\r\n"
    assert not (tmp_path / "artifacts").exists()


def test_task_evidence_keeps_anchors_and_rejects_stale_file_hashes(tmp_path: Path) -> None:
    file = tmp_path / "module.py"
    file.write_bytes(b"VALUE = 1\n")
    state = TaskEvidence()
    state.observe(
        ToolResult(
            "read",
            "operation",
            "read_file",
            "ok",
            "arbitrary file content",
            {"path": file.name, "sha256": sha256_bytes(file.read_bytes())},
        ),
        7,
    )
    before = state.project(Workspace(tmp_path), "user request")
    assert before and before["files"][0]["event_seq"] == 7
    assert "arbitrary file" not in json.dumps(before)
    file.write_bytes(b"VALUE = 2\n")
    after = state.project(Workspace(tmp_path), "user request")
    assert after and after["files"] == [] and after["stale_paths"] == 1


async def test_guidance_and_evidence_leave_existing_model_limits_in_force(tmp_path: Path) -> None:
    root = repo(tmp_path / "repo")
    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(ToolCall("read", "read_file", '{"path":"a.txt"}'),),
                stop_reason="tool_calls",
            ),
            AssistantReply("Checked."),
        ]
    )
    config = AppConfig(
        harness=HarnessConfig(task_evidence=True, budget_guidance=True),
        limits=LimitsConfig(max_model_calls=2),
    )
    with SessionStore(root, tmp_path / "journal") as store:
        agent = build_agent(config, model, store, stream=False)
        outcome = await agent.run("Read and report.")
        events = store.load_events()
    assert outcome.model_calls == 2
    assert any(item.type == "task.evidence" and item.payload["files"] for item in events)
    assert any(
        item.type == "budget.guidance" and item.payload["capacity_limited"] for item in events
    )
    assert any("Derived tool evidence" in message.content for message in model.requests[1].messages)
