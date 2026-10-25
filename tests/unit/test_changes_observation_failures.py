"""Unconfirmed Git subprocesses must stop setup and preserve their evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from stellarweaver import changes
from stellarweaver.changes import ChangeObservationUnknown, ChangesManager
from stellarweaver.domain import AssistantReply, JSONDict
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.git import GitDiffArgs, GitStatusArgs
from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry
from stellarweaver.tools.workspace import ToolError


def failure(kind: str) -> JSONDict:
    status, code = {
        "unknown": ("unknown", "JOB_CLEANUP_UNCONFIRMED"),
        "cancelled": ("cancelled", "COMMAND_CANCELLED"),
        "containment": ("error", "PROCESS_CONTAINMENT_FAILED"),
        "containment_exception": ("error", "PROCESS_CONTAINMENT_FAILED"),
    }[kind]
    return {
        "status": status,
        "content": "Git subprocess did not finish safely",
        "data": {"code": code, "process_management": {"cleanup_confirmed": False}},
    }


@pytest.mark.parametrize("stage", ["status", "staged_diff", "unstaged_diff", "current_paths_diff"])
@pytest.mark.parametrize("kind", ["unknown", "cancelled", "containment", "containment_exception"])
async def test_git_observation_failure_stops_at_each_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, kind: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "untracked.txt"
    target.write_bytes(b"user baseline\n")
    calls: list[str] = []
    enabled = stage != "current_paths_diff"
    expected = failure(kind)

    def prepared(label: str) -> PreparedTool:
        async def execute() -> JSONDict:
            calls.append(label)
            if enabled and label == stage:
                if kind == "containment_exception":
                    raise ToolError(
                        expected["data"]["code"], "Containment refused", expected["data"]
                    )
                return expected
            return {
                "status": "ok",
                "content": "",
                "data": {
                    "staged": [],
                    "unstaged": [],
                    "untracked": [target.name],
                    "diff": "",
                },
            }

        return PreparedTool({}, execute)

    async def status(args: GitStatusArgs, context: ToolContext) -> PreparedTool:
        return prepared("status")

    async def diff(args: GitDiffArgs, context: ToolContext) -> PreparedTool:
        return prepared(
            "current_paths_diff"
            if args.paths
            else "staged_diff"
            if args.staged
            else "unstaged_diff"
        )

    monkeypatch.setattr(changes, "prepare_git_status", status)
    monkeypatch.setattr(changes, "prepare_git_diff", diff)
    with SessionStore(root, tmp_path / "state") as store:
        published = []
        manager = ChangesManager(
            root, store, PolicyEngine(workspace=root), on_event=published.append
        )
        if stage == "current_paths_diff":
            await manager.capture_baseline()
            target.write_bytes(b"later user content\n")
            enabled = True
            calls.clear()
        before = target.read_bytes()
        with pytest.raises(ChangeObservationUnknown) as raised:
            if stage == "current_paths_diff":
                await manager.diff()
            else:
                await manager.capture_baseline()
        assert raised.value.status == expected["status"]
        assert raised.value.code == expected["data"]["code"]
        stages = ["status", "staged_diff", "unstaged_diff", "current_paths_diff"]
        assert calls == stages[: stages.index(stage) + 1]
        event = store.load_events()[-1]
        assert event.type == "changes.observation_unknown"
        assert event == published[-1]
        assert event.payload == raised.value.data
        evidence = event.payload["data"]
        if kind == "containment_exception":
            evidence = evidence["details"]
        assert evidence["process_management"] == {"cleanup_confirmed": False}
        assert target.read_bytes() == before
        assert len([e for e in store.load_events() if e.type == "changes.baseline"]) == (
            1 if stage == "current_paths_diff" else 0
        )


@pytest.mark.parametrize("missing_executable", [True, False])
async def test_normal_git_unavailability_keeps_no_git_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_executable: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    async def status(args: GitStatusArgs, context: ToolContext) -> PreparedTool:
        async def execute() -> JSONDict:
            if missing_executable:
                raise FileNotFoundError("git executable unavailable")
            return {"status": "error", "data": {"code": "COMMAND_FAILED", "exit_code": 128}}

        return PreparedTool({}, execute)

    monkeypatch.setattr(changes, "prepare_git_status", status)
    with SessionStore(root, tmp_path / "state") as store:
        model = FakeModel([AssistantReply("Offline completion without Git")])
        registry = ToolRegistry(
            root, PolicyEngine(workspace=root), artifacts_dir=store.artifacts_dir
        )
        outcome = await Agent(model, registry, store=store).run("Inspect without Git")
        assert outcome.run_status == "completed"
        assert len(model.requests) == 1
        baseline = next(event for event in store.load_events() if event.type == "changes.baseline")
        assert baseline.payload["git_available"] is False
        assert not any(e.type == "changes.observation_unknown" for e in store.load_events())


@pytest.mark.parametrize("kind", ["unknown", "cancelled", "containment"])
async def test_agent_setup_does_not_call_model_after_unconfirmed_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    marker = root / "user.txt"
    marker.write_bytes(b"preserve this user content\n")
    expected = failure(kind)

    async def status(args: GitStatusArgs, context: ToolContext) -> PreparedTool:
        async def execute() -> JSONDict:
            return expected

        return PreparedTool({}, execute)

    monkeypatch.setattr(changes, "prepare_git_status", status)
    with SessionStore(root, tmp_path / "state") as store:
        model = FakeModel([AssistantReply("Must not be requested")])
        registry = ToolRegistry(
            root, PolicyEngine(workspace=root), artifacts_dir=store.artifacts_dir
        )
        outcome = await Agent(model, registry, store=store).run(
            "Stop if Git cleanup is unconfirmed"
        )
        assert outcome.run_status == ("cancelled" if kind == "cancelled" else "failed")
        assert model.requests == []
        assert expected["data"]["code"] in outcome.reason
        assert not any(e.type == "changes.baseline" for e in store.load_events())
        assert not store.load_results()
        assert marker.read_bytes() == b"preserve this user content\n"
