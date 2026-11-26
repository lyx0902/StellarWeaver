"""Unit tests for Harness Phase 2 (Tasks 2.1 - 2.4)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from stellarweaver.config import (
    AppConfig,
    HarnessConfig,
    ModelConfig,
    PolicyConfig,
    RetryConfig,
    long_horizon_deepseek_flash,
)
from stellarweaver.domain import (
    AgentEvent,
    AssistantReply,
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
    ToolCall,
    ToolResult,
)
from stellarweaver.evals import swebench
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.streaming import REPETITION_ERROR_MESSAGE, ChatCompletionAccumulator
from stellarweaver.runtime.loop import Agent
from stellarweaver.runtime.repair import RepairController
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Task 2.1: Relaxed bounds, DeepSeek timeout, CLI --reasoning-effort, preset
# ---------------------------------------------------------------------------


def test_retry_and_timeout_limits_relaxed(tmp_path: Path) -> None:
    retry = RetryConfig(max_retries=8, base_delay_seconds=1.0, max_delay_seconds=120.0)
    assert retry.max_retries == 8
    assert retry.max_delay_seconds == 120.0

    with pytest.raises(ValidationError):
        RetryConfig(max_retries=9)
    with pytest.raises(ValidationError):
        RetryConfig(max_delay_seconds=121.0)

    assert ModelConfig(provider="deepseek", thinking="enabled").request_timeout_seconds == 240
    assert ModelConfig(provider="deepseek", thinking="disabled").request_timeout_seconds == 90
    assert ModelConfig(provider="fake", thinking="enabled").request_timeout_seconds == 90
    assert ModelConfig(request_timeout_seconds=900).request_timeout_seconds == 900
    with pytest.raises(ValidationError):
        ModelConfig(request_timeout_seconds=901)

    engine = PolicyEngine("read-only", workspace=tmp_path)
    registry = ToolRegistry(tmp_path, engine)
    agent = Agent(FakeModel([]), registry, max_retries=8)
    assert agent.max_retries == 8
    with pytest.raises(ValueError, match="between 0 and 8"):
        Agent(FakeModel([]), registry, max_retries=9)


def test_long_horizon_deepseek_flash_preset() -> None:
    cfg = long_horizon_deepseek_flash()
    assert cfg.model.provider == "deepseek"
    assert cfg.model.model == "deepseek-flash"
    assert cfg.model.thinking == "enabled"
    assert cfg.model.reasoning_effort == "max"
    assert cfg.model.context_window_tokens == 512_000
    assert cfg.model.max_output_tokens == 65_536
    assert cfg.model.request_timeout_seconds == 240
    assert cfg.model.supports_streaming is True
    assert cfg.limits.max_model_calls is None
    assert cfg.limits.max_tool_calls == 300
    assert cfg.limits.max_wall_seconds == 3600
    assert cfg.limits.max_input_tokens_total == 80_000_000
    assert cfg.limits.max_output_tokens_total == 2_000_000
    assert cfg.retry.max_retries == 5
    assert cfg.retry.max_delay_seconds == 60.0
    assert cfg.policy.preset == "benchmark"
    assert cfg.harness.sticky_context_checkpoint is True
    assert cfg.harness.include_linter is False
    assert cfg.harness.max_length_continuations == 3
    assert cfg.harness.repeated_failure_soft_threshold == 3
    assert cfg.harness.repeated_failure_hard_threshold == 6
    assert cfg.harness.benchmark_allow_denied_tool_continue is True
    assert cfg.harness.stream_repetition_detection is True
    assert cfg.harness.stream_repetition_max_retries == 2


def test_harness_config_rejects_hard_below_soft() -> None:
    with pytest.raises(ValidationError, match="repeated_failure_hard_threshold"):
        HarnessConfig(repeated_failure_soft_threshold=4, repeated_failure_hard_threshold=3)


def test_swebench_cli_reasoning_effort_frozen_in_manifest(tmp_path: Path) -> None:
    cli = swebench.parser()
    parsed = cli.parse_args(
        [
            "prepare",
            "--dataset",
            str(tmp_path / "dev.parquet"),
            "--dataset-sha256",
            "a" * 64,
            "--output",
            str(tmp_path / "out"),
            "--grader-python",
            str(tmp_path / "python"),
            "--reasoning-effort",
            "max",
        ]
    )
    assert parsed.reasoning_effort == "max"

    manifest = {
        "limits": {**swebench.LIMITS, "max_model_calls": None},
        "context_window_tokens": 512_000,
        "model": "deepseek-flash",
        "thinking": "enabled",
        "reasoning_effort": "max",
    }
    full_args = SimpleNamespace(harness_profile="full", live=True)
    full_cfg = swebench.RunConfiguration.from_manifest(full_args, manifest)
    assert full_cfg.app.model.reasoning_effort == "max"
    assert full_cfg.app.harness.max_length_continuations == 3
    assert full_cfg.app.harness.repeated_failure_soft_threshold == 3
    assert full_cfg.app.harness.repeated_failure_hard_threshold == 6
    assert full_cfg.app.harness.benchmark_allow_denied_tool_continue is True
    assert full_cfg.app.harness.stream_repetition_detection is True

    base_args = SimpleNamespace(harness_profile="baseline", live=False)
    base_cfg = swebench.RunConfiguration.from_manifest(base_args, manifest)
    assert base_cfg.app.model.reasoning_effort == "max"
    assert base_cfg.app.harness.max_length_continuations == 0
    assert base_cfg.app.harness.repeated_failure_hard_threshold == 3
    assert base_cfg.app.harness.benchmark_allow_denied_tool_continue is False
    assert base_cfg.app.harness.stream_repetition_detection is False


# ---------------------------------------------------------------------------
# Task 2.2: stop_reason in {"length", "max_tokens"} safe continuation
# ---------------------------------------------------------------------------


async def test_length_continuation_truncated_text_recovers(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    model = FakeModel(
        [
            AssistantReply(text="Partial answer...", stop_reason="length"),
            AssistantReply(text="Complete answer.", stop_reason="stop"),
        ]
    )
    config = AppConfig(
        policy=PolicyConfig(preset="read-only"),
        harness=HarnessConfig(max_length_continuations=3),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(config, model, store, on_event=events.append)
        outcome = await agent.run("Explain this repo")

    assert outcome.run_status == "completed"
    assert outcome.final_text == "Complete answer."
    cont_events = [e for e in events if e.type == "model.length_continuation"]
    assert len(cont_events) == 1
    assert cont_events[0].payload == {"attempt": 1, "reason": "truncated_text"}


async def test_length_continuation_drops_truncated_invalid_tool_call(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    model = FakeModel(
        [
            AssistantReply(
                text="Writing file...",
                tool_calls=(
                    ToolCall("c1", "create_file", '{"path": "bad.txt", "content": "cut off'),
                ),
                stop_reason="length",
            ),
            AssistantReply(text="Recovered without executing broken tool.", stop_reason="stop"),
        ]
    )
    config = AppConfig(
        policy=PolicyConfig(preset="workspace-edit"),
        harness=HarnessConfig(max_length_continuations=3),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(config, model, store, on_event=events.append)
        outcome = await agent.run("Create bad.txt")

    assert outcome.run_status == "completed"
    assert not (tmp_path / "bad.txt").exists()
    assert agent.results == []
    cont_events = [e for e in events if e.type == "model.length_continuation"]
    assert len(cont_events) == 1
    assert cont_events[0].payload == {
        "attempt": 1,
        "reason": "truncated_tool_calls",
        "dropped_tool_calls": 1,
    }


async def test_length_continuation_pure_thinking_exhaustion_uses_non_empty_placeholder(
    tmp_path: Path,
) -> None:
    events: list[AgentEvent] = []
    seen_requests: list[ModelRequest] = []

    def respond(req: ModelRequest) -> AssistantReply:
        seen_requests.append(req)
        if len(seen_requests) == 1:
            return AssistantReply(
                text="",
                tool_calls=(),
                stop_reason="length",
                reasoning_content="Long internal chain of thought that hit the output limit...",
            )
        return AssistantReply(text="Concise final answer.", stop_reason="stop")

    model = FakeModel(respond)
    config = AppConfig(
        policy=PolicyConfig(preset="read-only"),
        harness=HarnessConfig(max_length_continuations=3),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(config, model, store, on_event=events.append)
        outcome = await agent.run("Solve puzzle")
        saved_messages = store.load_messages()

    assert outcome.run_status == "completed"
    assert outcome.final_text == "Concise final answer."
    thinking_events = [e for e in events if e.type == "model.truncated_thinking"]
    assert len(thinking_events) == 1
    assert thinking_events[0].payload == {"attempt": 1, "stop_reason": "length"}

    # No empty assistant message is ever committed or sent to the model.
    assistant_msgs = [m for m in saved_messages if m.role == "assistant"]
    assert all(m.content.strip() or m.tool_calls for m in assistant_msgs)
    assert assistant_msgs[0].content == "[Thinking truncated at output token limit.]"

    # Second request maintains strict role alternation: placeholder assistant -> follow-up user.
    second_roles = [m.role for m in seen_requests[1].messages]
    assert second_roles[-2:] == ["assistant", "user"]
    assert seen_requests[1].messages[-2].content == "[Thinking truncated at output token limit.]"


async def test_length_continuation_executes_valid_tool_call_on_length(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
    events: list[AgentEvent] = []
    model = FakeModel(
        [
            AssistantReply(
                text="",
                tool_calls=(ToolCall("c1", "list_files", "{}"),),
                stop_reason="max_tokens",
            ),
            AssistantReply(text="Found hello.txt.", stop_reason="stop"),
        ]
    )
    config = AppConfig(
        policy=PolicyConfig(preset="read-only"),
        harness=HarnessConfig(max_length_continuations=3),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(config, model, store, on_event=events.append)
        outcome = await agent.run("List files")

    assert outcome.run_status == "completed"
    assert len(agent.results) == 1
    assert agent.results[0].status == "ok"
    cont_events = [e for e in events if e.type == "model.length_continuation"]
    assert len(cont_events) == 1
    assert cont_events[0].payload == {"attempt": 1, "reason": "complete_tool_calls_on_length"}


async def test_length_continuation_respects_max_cap(tmp_path: Path) -> None:
    events: list[AgentEvent] = []
    model = FakeModel(
        [
            AssistantReply(text="cut 1", stop_reason="length"),
            AssistantReply(text="cut 2", stop_reason="length"),
            AssistantReply(text="cut 3", stop_reason="length"),
            AssistantReply(text="cut 4", stop_reason="length"),
        ]
    )
    config = AppConfig(
        policy=PolicyConfig(preset="read-only"),
        harness=HarnessConfig(max_length_continuations=3),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(config, model, store, on_event=events.append)
        outcome = await agent.run("Keep truncating")

    assert outcome.run_status == "failed"
    assert outcome.reason == "incomplete model response: length"
    assert outcome.model_calls == 4
    cont_events = [e for e in events if e.type == "model.length_continuation"]
    assert len(cont_events) == 3


# ---------------------------------------------------------------------------
# Task 2.3: 2-tier failure soft guidance (in tool tail) + benchmark denied
# ---------------------------------------------------------------------------


async def test_two_tier_repeated_failure_soft_guidance_in_tool_result_then_hard_stop(
    tmp_path: Path,
) -> None:
    events: list[AgentEvent] = []
    bad_args = json.dumps(
        {
            "path": "missing.py",
            "old_text": "a",
            "new_text": "b",
            "expected_sha256": "0" * 64,
        }
    )
    model = FakeModel(
        [
            AssistantReply(
                text="",
                tool_calls=(ToolCall(f"c{i}", "edit_file", bad_args),),
                stop_reason="tool_calls",
            )
            for i in range(1, 8)
        ]
    )
    config = AppConfig(
        policy=PolicyConfig(preset="workspace-edit"),
        harness=HarnessConfig(
            repair_controller="feedback",
            repeated_failure_soft_threshold=3,
            repeated_failure_hard_threshold=6,
        ),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(config, model, store, on_event=events.append)
        outcome = await agent.run("Edit missing.py")

    assert outcome.run_status == "failed"
    assert outcome.reason == "6 identical tool failures"
    assert len(agent.results) == 6
    tool_msgs = [json.loads(m.content)["content"] for m in agent.messages if m.role == "tool"]
    assert len(tool_msgs) == 6
    # Calls 1 and 2: no soft guidance yet.
    assert "[Harness diagnostic:" not in tool_msgs[0]
    assert "[Harness diagnostic:" not in tool_msgs[1]
    # Calls 3, 4, 5: soft guidance appended to the tail of the tool message content sent to model.
    for idx in (2, 3, 4):
        assert "[Harness diagnostic:" in tool_msgs[idx]
    # Call 6: hits hard threshold directly without extra soft guidance.
    assert "[Harness diagnostic:" not in tool_msgs[5]
    # Audit ToolResult objects remain unmodified.
    assert all("[Harness diagnostic:" not in r.content for r in agent.results)

    soft_events = [e for e in events if e.type == "repair.soft_guidance"]
    assert [e.payload["repeat_count"] for e in soft_events] == [3, 4, 5]
    # Soft guidance must NOT inject any extra user message (preserves DeepSeek Thinking turn).
    user_messages = [m for m in agent.messages if m.role == "user"]
    assert len(user_messages) == 1


def test_repair_controller_enforce_respects_hard_threshold() -> None:
    controller = RepairController(enforce=True)
    result = ToolResult(
        call_id="c1",
        operation_id="op1",
        tool_name="run_tests",
        status="error",
        content="AssertionError: expected 1 == 2",
        data={"exit_code": 1},
    )
    for attempt in range(1, 6):
        event, stalled = controller.observe(result, command=True, hard_threshold=6)
        assert event is not None
        assert event["repeat_count"] == attempt
        assert stalled is False
    event, stalled = controller.observe(result, command=True, hard_threshold=6)
    assert event is not None
    assert event["repeat_count"] == 6
    assert stalled is True


async def test_benchmark_allow_denied_tool_continue_scoped_strictly_to_benchmark(
    tmp_path: Path,
) -> None:
    # 1. Under preset="benchmark", denied tool returns error/denied result and lets model continue.
    bench_model = FakeModel(
        [
            AssistantReply(
                text="",
                tool_calls=(ToolCall("c1", "run_command", json.dumps({"argv": ["echo", "hi"]})),),
                stop_reason="tool_calls",
            ),
            AssistantReply(text="Handled denial gracefully.", stop_reason="stop"),
        ]
    )
    bench_config = AppConfig(
        policy=PolicyConfig(preset="benchmark"),
        harness=HarnessConfig(benchmark_allow_denied_tool_continue=True),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(bench_config, bench_model, store)
        bench_outcome = await agent.run("Try running a disallowed command")

    assert bench_outcome.run_status == "completed"
    assert bench_outcome.final_text == "Handled denial gracefully."
    assert len(agent.results) == 1
    assert agent.results[0].status == "denied"

    # 2. Under preset="ask" and preset="auto", explicit user denial MUST still stop immediately,
    # even if benchmark_allow_denied_tool_continue=True is set.
    async def deny_all(_req: dict[str, Any]) -> str:
        return "deny"

    for preset in ("ask", "auto"):
        call = (
            ToolCall("c1", "create_file", json.dumps({"path": f"{preset}.txt", "content": "x"}))
            if preset == "ask"
            else ToolCall("c1", "run_command", json.dumps({"argv": ["echo", "hi"]}))
        )
        interactive_model = FakeModel(
            [
                AssistantReply(text="", tool_calls=(call,), stop_reason="tool_calls"),
                AssistantReply(text="Should never reach this turn.", stop_reason="stop"),
            ]
        )
        cfg = AppConfig(
            policy=PolicyConfig(preset=preset),
            harness=HarnessConfig(benchmark_allow_denied_tool_continue=True),
        )
        with SessionStore(tmp_path) as store:
            agent = build_agent(
                cfg,
                interactive_model,
                store,
                interactive=True,
                approval=deny_all,
            )
            outcome = await agent.run("Perform action requiring approval")
        assert outcome.run_status == "blocked", preset
        assert outcome.model_calls == 1, preset
        assert agent.results[0].status == "denied", preset


# ---------------------------------------------------------------------------
# Task 2.4: Stream repetition detection (reasoning_content only) + retry cap
# ---------------------------------------------------------------------------


def _chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
) -> Any:
    delta = SimpleNamespace(
        role="assistant",
        content=content,
        reasoning_content=reasoning_content,
        function_call=None,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(
        choices=[choice],
        usage=None,
        model="deepseek-flash",
        system_fingerprint="fp-test",
    )


def test_accumulator_detects_reasoning_phrase_and_line_repetition() -> None:
    # 1. Repeated short phrase inside reasoning_content.
    acc_phrase = ChatCompletionAccumulator(
        max_output_tokens=16384,
        capture_reasoning=True,
        detect_repetition=True,
    )
    unit = "Wait, let me re-check that index. "
    with pytest.raises(ProviderError, match="repetitive generation loop") as exc_info:
        for _ in range(60):
            acc_phrase.feed(_chunk(reasoning_content=unit))
    assert exc_info.value.retryable is True
    assert exc_info.value.partial_stream is True

    # 2. Repeated identical lines inside reasoning_content.
    acc_line = ChatCompletionAccumulator(
        max_output_tokens=16384,
        capture_reasoning=True,
        detect_repetition=True,
    )
    line = "Therefore we must re-verify line 42 in parser.py.\n"
    with pytest.raises(ProviderError, match="repetitive generation loop") as line_exc:
        for _ in range(40):
            acc_line.feed(_chunk(reasoning_content=line))
    assert line_exc.value.retryable is True
    assert line_exc.value.partial_stream is True


def test_accumulator_does_not_false_positive_on_visible_content_or_tool_calls() -> None:
    acc = ChatCompletionAccumulator(
        max_output_tokens=16384,
        capture_reasoning=True,
        detect_repetition=True,
    )
    # Normal short non-repetitive reasoning.
    acc.feed(_chunk(reasoning_content="The user wants a repeated markdown table of 60 rows."))
    # Heavily repetitive visible content (must NOT trigger repetition detector).
    table_row = "| col_a | col_b | col_c | 0 |\n"
    for _ in range(80):
        acc.feed(_chunk(content=table_row))
    # Heavily repetitive tool_call arguments (must NOT trigger repetition detector).
    tc_init = SimpleNamespace(
        index=0,
        id="call_1",
        type="function",
        function=SimpleNamespace(name="create_file", arguments='{"path":"t.txt","content":"'),
    )
    acc.feed(_chunk(tool_calls=[tc_init]))
    for _ in range(80):
        tc_part = SimpleNamespace(
            index=0,
            id=None,
            type=None,
            function=SimpleNamespace(name=None, arguments="item_x, "),
        )
        acc.feed(_chunk(tool_calls=[tc_part]))
    tc_end = SimpleNamespace(
        index=0,
        id=None,
        type=None,
        function=SimpleNamespace(name=None, arguments='"}'),
    )
    acc.feed(_chunk(tool_calls=[tc_end], finish_reason="tool_calls"))
    reply = acc.finish()
    assert reply.stop_reason == "tool_calls"
    assert len(reply.tool_calls) == 1


class _RepetitionStreamProvider:
    """Streaming provider that fails with stream repetition N times before succeeding."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.seen_detect_flags: list[bool] = []

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            tool_calling=True,
            streaming=True,
            context_window_tokens=200_000,
        )

    async def complete(self, request: ModelRequest) -> AssistantReply:
        raise AssertionError("Expected streaming path")

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.calls += 1
        self.seen_detect_flags.append(request.detect_repetition)
        if self.calls <= self.fail_times:
            acc = ChatCompletionAccumulator(
                max_output_tokens=request.max_output_tokens,
                capture_reasoning=True,
                detect_repetition=request.detect_repetition,
            )
            for _ in range(60):
                acc.feed(_chunk(reasoning_content="Looping thought fragment again. "))
            raise AssertionError(f"Expected {REPETITION_ERROR_MESSAGE}")
        yield ModelStreamEvent(
            "response.completed",
            reply=AssistantReply(text="Recovered after repetition retry.", stop_reason="stop"),
        )


async def test_agent_retries_stream_repetition_and_enforces_repetition_retry_cap(
    tmp_path: Path,
) -> None:
    # 1. Fails twice with repetition, succeeds on 3rd attempt (within stream_repetition_max_retries=2).
    events_ok: list[AgentEvent] = []
    provider_ok = _RepetitionStreamProvider(fail_times=2)
    config = AppConfig(
        model=ModelConfig(supports_streaming=True),
        policy=PolicyConfig(preset="read-only"),
        retry=RetryConfig(max_retries=5, base_delay_seconds=0.0, max_delay_seconds=1.0),
        harness=HarnessConfig(
            stream_repetition_detection=True,
            stream_repetition_max_retries=2,
        ),
    )
    with SessionStore(tmp_path) as store:
        agent = build_agent(config, provider_ok, store, on_event=events_ok.append)
        outcome_ok = await agent.run("Think carefully")

    assert outcome_ok.run_status == "completed"
    assert outcome_ok.final_text == "Recovered after repetition retry."
    assert provider_ok.calls == 3
    assert provider_ok.seen_detect_flags == [True, True, True]
    assert len([e for e in events_ok if e.type == "model.interrupted"]) == 2
    assert len([e for e in events_ok if e.type == "model.retry_scheduled"]) == 2

    # 2. Fails 3+ times with repetition: stops after 2 repetition retries even though max_retries=5.
    events_cap: list[AgentEvent] = []
    provider_cap = _RepetitionStreamProvider(fail_times=5)
    with SessionStore(tmp_path) as store:
        agent_cap = build_agent(config, provider_cap, store, on_event=events_cap.append)
        outcome_cap = await agent_cap.run("Loop forever")

    assert outcome_cap.run_status == "failed"
    assert REPETITION_ERROR_MESSAGE in outcome_cap.reason
    assert provider_cap.calls == 3  # 1 initial + 2 repetition retries (capped below max_retries=5)
    assert len([e for e in events_cap if e.type == "model.retry_scheduled"]) == 2
