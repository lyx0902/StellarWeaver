"""Public API boundaries remain safe independently of CLI validation."""

import asyncio
from pathlib import Path

import pytest

from stellarweaver.changes import ChangesManager
from stellarweaver.config import LimitsConfig
from stellarweaver.domain import AssistantReply
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def registry(root: Path, store: SessionStore) -> ToolRegistry:
    return ToolRegistry(
        root, PolicyEngine("read-only", workspace=root), artifacts_dir=store.artifacts_dir
    )


def test_direct_api_cannot_restore_one_workspace_into_another(tmp_path: Path) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    with SessionStore(first, tmp_path / "state") as store:
        with pytest.raises(ValueError, match="workspace"):
            Agent(FakeModel(), registry(second, store), store=store)


async def test_clear_boundary_survives_resume_and_requires_new_goal(tmp_path: Path) -> None:
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = Agent(
            FakeModel([AssistantReply("Old final response")]),
            registry(tmp_path, store),
            store=store,
        )
        assert (await agent.run("OLD task")).run_status == "completed"
        original = len(store.load_messages())
        await agent.clear_context()
        identifier = store.session_id
        assert len(store.load_messages()) == original
    with SessionStore.open(identifier, workspace=tmp_path, data_dir=tmp_path / "state") as store:
        model = FakeModel([AssistantReply("New response")])
        agent = Agent(model, registry(tmp_path, store), store=store)
        result = await agent.resume()
        assert result.run_status == "blocked" and not model.requests
        assert "OLD task" not in repr(agent.projected_history())
        result = await agent.continue_run("NEW task")
        assert result.run_status == "completed"
        assert all("OLD task" not in message.content for message in model.requests[-1].messages)
        assert len(store.load_messages()) > original


async def test_baseline_capture_observes_total_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def slow(self: ChangesManager) -> dict:
        await asyncio.sleep(10)
        return {}

    monkeypatch.setattr(ChangesManager, "capture_baseline", slow)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        model = FakeModel()
        agent = Agent(
            model, registry(tmp_path, store), store=store, limits=LimitsConfig(max_wall_seconds=1)
        )
        result = await asyncio.wait_for(agent.run("budgeted task"), timeout=3)
        assert result.run_status == "budget_exceeded"
        assert result.model_calls == 0 and not model.requests
