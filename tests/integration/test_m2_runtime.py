"""M2 stream/retry regressions use fake decisions with real file effects."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from stellarweaver.config import LimitsConfig
from stellarweaver.domain import (
    AssistantReply,
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
    TextDelta,
    ToolCall,
)
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


class ScriptedStreamModel:
    def __init__(
        self, sequences: Sequence[Sequence[ModelStreamEvent | ProviderError]], root: Path
    ) -> None:
        self.sequences = list(sequences)
        self.requests: list[ModelRequest] = []
        self.root = root

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(streaming=True)

    async def complete(self, request: ModelRequest) -> AssistantReply:
        raise AssertionError("A streaming-capable provider must use stream in this test")

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.requests.append(request)
        sequence = self.sequences.pop(0)
        for event in sequence:
            # No side effect can occur while this reply is still being received.
            if len(self.requests) <= 2:
                assert not (self.root / "created.txt").exists()
            if isinstance(event, ProviderError):
                raise event
            yield event


def create_reply(*, stop_reason: str = "tool_calls") -> AssistantReply:
    return AssistantReply(
        tool_calls=(
            ToolCall("create-once", "create_file", '{"path":"created.txt","content":"once"}'),
        ),
        stop_reason=stop_reason,
    )


def registry(root: Path, store: SessionStore) -> ToolRegistry:
    return ToolRegistry(
        root,
        PolicyEngine("workspace-edit", workspace=root),
        artifacts_dir=store.artifacts_dir,
    )


async def test_interrupted_stream_retries_without_committing_partial_or_tools(
    tmp_path: Path,
) -> None:
    model = ScriptedStreamModel(
        [
            [
                ModelStreamEvent("text.delta", text="uncommitted draft"),
                ProviderError("connection lost", retryable=True, partial_stream=True),
            ],
            [
                ModelStreamEvent("text.delta", text="complete replacement"),
                ModelStreamEvent("response.completed", reply=create_reply()),
            ],
            [ModelStreamEvent("response.completed", reply=AssistantReply(text="Done."))],
        ],
        tmp_path,
    )
    deltas: list[TextDelta] = []
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = Agent(
            model,
            registry(tmp_path, store),
            store=store,
            stream=True,
            on_delta=deltas.append,
            retry_base_seconds=0,
        )
        outcome = await agent.run("Create a file once.")
        assert outcome.run_status == "completed"
        assert outcome.model_calls == 3
        assert outcome.tool_calls == 1
        assert outcome.usage_incomplete is True
        assert (tmp_path / "created.txt").read_text() == "once"
        assert len(agent.results) == 1
        assert "uncommitted draft" not in repr(agent.messages)
        assert "uncommitted draft" not in repr(model.requests[1].messages)
        assert any(delta.text == "uncommitted draft" for delta in deltas)
        assert all(delta.session_id == outcome.session_id for delta in deltas)
        assert len({(d.run_id, d.request_index, d.chunk_index) for d in deltas}) == len(deltas)
        bodies = [row[0] for row in store.connection.execute("SELECT body_json FROM messages")]
        assert not any("uncommitted draft" in body for body in bodies)
        assert store.connection.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 1


async def test_stream_ending_without_completed_event_has_no_side_effects(tmp_path: Path) -> None:
    model = ScriptedStreamModel(
        [[ModelStreamEvent("text.delta", text="incomplete response")]], tmp_path
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(
            model, registry(tmp_path, store), store=store, stream=True, max_retries=0
        ).run("Create a file.")
        assert outcome.run_status == "failed"
        assert outcome.tool_calls == 0
        assert not (tmp_path / "created.txt").exists()
        assert store.connection.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 0


async def test_stream_length_finish_never_executes_complete_looking_call(tmp_path: Path) -> None:
    model = ScriptedStreamModel(
        [[ModelStreamEvent("response.completed", reply=create_reply(stop_reason="length"))]],
        tmp_path,
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = Agent(model, registry(tmp_path, store), store=store, stream=True)
        outcome = await agent.run("Create a file.")
        assert outcome.run_status == "failed"
        assert outcome.tool_calls == 0
        assert not (tmp_path / "created.txt").exists()
        assert len(agent.results) == 1
        assert agent.results[0].status == "blocked"


async def test_stream_request_falls_back_to_complete_for_capability_false(tmp_path: Path) -> None:
    model = FakeModel([AssistantReply(text="Non-streaming fallback completed.")])
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(model, registry(tmp_path, store), store=store, stream=True).run(
            "Say hello."
        )
        assert outcome.run_status == "completed"
        assert outcome.final_text == "Non-streaming fallback completed."
        assert len(model.requests) == 1


@pytest.mark.parametrize(
    ("status_code", "retryable", "expected_calls", "expected_status"),
    [(401, False, 1, "failed"), (429, True, 2, "completed")],
)
async def test_retryability_controls_actual_requests(
    tmp_path: Path, status_code: int, retryable: bool, expected_calls: int, expected_status: str
) -> None:
    attempts = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderError(
                "Provider rejected request",
                status_code=status_code,
                retryable=retryable,
                retry_after_seconds=0,
            )
        return AssistantReply(text="Retried successfully.")

    model = FakeModel(script)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(
            model, registry(tmp_path, store), store=store, retry_base_seconds=0
        ).run("Try once and retry only transient failures.")
        assert outcome.run_status == expected_status
        assert outcome.model_calls == len(model.requests) == expected_calls
        assert outcome.usage_incomplete is True


async def test_retry_is_blocked_by_model_call_budget(tmp_path: Path) -> None:
    def fail(request: ModelRequest) -> AssistantReply:
        raise ProviderError("rate limited", retryable=True, status_code=429)

    model = FakeModel(fail)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(
            model,
            registry(tmp_path, store),
            store=store,
            limits=LimitsConfig(max_model_calls=1),
            retry_base_seconds=0,
        ).run("Cannot issue a second request.")
        assert outcome.run_status == "budget_exceeded"
        assert outcome.model_calls == len(model.requests) == 1
        assert "max_model_calls" in outcome.reason


async def test_retry_attempt_limit_is_two_additional_requests(tmp_path: Path) -> None:
    def fail(request: ModelRequest) -> AssistantReply:
        raise ProviderError("temporarily unavailable", retryable=True, status_code=503)

    model = FakeModel(fail)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(
            model, registry(tmp_path, store), store=store, retry_base_seconds=0
        ).run("Stop after bounded retries.")
        assert outcome.run_status == "failed"
        assert outcome.model_calls == len(model.requests) == 3
        assert outcome.usage_incomplete is True


async def test_failed_request_reserves_unknown_output_before_retry(tmp_path: Path) -> None:
    def fail(request: ModelRequest) -> AssistantReply:
        raise ProviderError("connection lost before usage", retryable=True)

    model = FakeModel(fail)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(
            model,
            registry(tmp_path, store),
            store=store,
            limits=LimitsConfig(max_output_tokens_total=16),
            max_output_tokens=16,
            retry_base_seconds=0,
        ).run("Unknown failed usage must still consume the request reservation.")
        assert outcome.run_status == "budget_exceeded"
        assert outcome.model_calls == len(model.requests) == 1
        assert outcome.output_tokens == 16
        assert outcome.usage_incomplete is True
        assert "max_output_tokens_total" in outcome.reason


async def test_reported_failure_usage_counts_before_successful_retry(tmp_path: Path) -> None:
    def script(request: ModelRequest) -> AssistantReply:
        if len(model.requests) == 1:
            raise ProviderError(
                "temporarily unavailable after processing",
                retryable=True,
                usage={"prompt_tokens": 10, "completion_tokens": 2},
            )
        assert request.max_output_tokens == 1
        return AssistantReply(text="done", usage={"prompt_tokens": 12, "completion_tokens": 1})

    model = FakeModel(script)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(
            model,
            registry(tmp_path, store),
            store=store,
            limits=LimitsConfig(max_output_tokens_total=3),
            max_output_tokens=3,
            retry_base_seconds=0,
        ).run("Count the provider's usage on failed requests too.")
        assert outcome.run_status == "completed"
        assert outcome.model_calls == len(model.requests) == 2
        assert outcome.input_tokens == 22
        assert outcome.output_tokens == 3
        assert outcome.usage_incomplete is False


async def test_retry_after_cannot_outlive_remaining_wall_budget(tmp_path: Path) -> None:
    def fail(request: ModelRequest) -> AssistantReply:
        raise ProviderError("rate limited", retryable=True, status_code=429, retry_after_seconds=60)

    model = FakeModel(fail)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        started = time.monotonic()
        outcome = await asyncio.wait_for(
            Agent(
                model,
                registry(tmp_path, store),
                store=store,
                limits=LimitsConfig(max_wall_seconds=1),
                retry_base_seconds=0,
            ).run("Do not wait for a retry outside the budget."),
            timeout=2,
        )
        assert time.monotonic() - started < 1.5
        assert outcome.run_status == "budget_exceeded"
        assert outcome.model_calls == len(model.requests) == 1


async def test_backoff_deadline_records_budget_outcome_after_scheduling_delay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_sleep = asyncio.sleep

    async def delayed_wakeup(delay: float) -> None:
        await original_sleep(delay + 2)

    def fail(request: ModelRequest) -> AssistantReply:
        raise ProviderError("retry after transient connection error", retryable=True)

    monkeypatch.setattr("stellarweaver.runtime.loop.asyncio.sleep", delayed_wakeup)
    model = FakeModel(fail)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        outcome = await Agent(
            model,
            registry(tmp_path, store),
            store=store,
            limits=LimitsConfig(max_wall_seconds=1),
            retry_base_seconds=0,
        ).run("A delayed scheduler must still produce a persisted budget outcome.")
        assert outcome.run_status == "budget_exceeded"
        assert outcome.model_calls == len(model.requests) == 1
        saved = store.connection.execute("SELECT outcome_json FROM runs").fetchone()[0]
        assert json.loads(saved)["run_status"] == "budget_exceeded"


async def test_retry_after_completed_tool_does_not_reexecute_history(tmp_path: Path) -> None:
    def script(request: ModelRequest) -> AssistantReply:
        if len(model.requests) == 1:
            return create_reply()
        observed = [message for message in request.messages if message.role == "tool"]
        assert len(observed) == 1
        assert json.loads(observed[0].content)["status"] == "ok"
        if len(model.requests) == 2:
            raise ProviderError("retry this model request", retryable=True)
        return AssistantReply(text="The already completed file edit was not repeated.")

    model = FakeModel(script)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = Agent(model, registry(tmp_path, store), store=store, retry_base_seconds=0)
        outcome = await agent.run("Create one file, then report.")
        assert outcome.run_status == "completed"
        assert outcome.tool_calls == len(agent.results) == 1
        assert outcome.model_calls == 3
        assert (tmp_path / "created.txt").read_text() == "once"
        assert store.connection.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 1


async def test_call_id_reuse_across_rounds_cannot_overwrite_prior_execution(tmp_path: Path) -> None:
    (tmp_path / "original.txt").write_text("original", encoding="utf-8")
    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(ToolCall("reused-id", "read_file", '{"path":"original.txt"}'),),
                stop_reason="tool_calls",
            ),
            AssistantReply(
                tool_calls=(
                    ToolCall("reused-id", "create_file", '{"path":"created.txt","content":"no"}'),
                ),
                stop_reason="length",
            ),
        ]
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = Agent(model, registry(tmp_path, store), store=store)
        outcome = await agent.run("Read one file, then receive a truncated tool reply.")
        assert outcome.run_status == "failed"
        assert outcome.tool_calls == 1
        assert [result.status for result in agent.results] == ["ok", "blocked"]
        assert len({result.operation_id for result in agent.results}) == 2
        assert not (tmp_path / "created.txt").exists()
        rows = store.connection.execute(
            "SELECT assistant_message_id,call_id,state,result_json FROM tool_executions"
        ).fetchall()
        assert len(rows) == 2
        assert len({row[0] for row in rows}) == 2
        assert {row[2] for row in rows} == {"succeeded", "denied"}
        assert {json.loads(row[3])["tool_name"] for row in rows} == {"read_file", "create_file"}
