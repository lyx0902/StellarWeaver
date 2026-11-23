"""Replay long source-reading sessions against offline Thinking request payloads."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from statistics import median

import pytest

from stellarweaver.config import HarnessConfig
from stellarweaver.context import estimate_tokens
from stellarweaver.domain import (
    AgentEvent,
    AssistantReply,
    Message,
    ModelCapabilities,
    ModelRequest,
    ToolCall,
)
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import serialize_message
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry

WINDOW = 200_000
OUTPUT = 8_192
RESERVE = 2_048
BYTES_PER_TOKEN = 3.6


class ThinkingModel(FakeModel):
    def capabilities(self) -> ModelCapabilities:
        return replace(super().capabilities(), requires_reasoning_history=True)


def prompt_tokens(request: ModelRequest) -> int:
    raw = estimate_tokens(
        {"messages": [asdict(message) for message in request.messages], "tools": request.tools}
    )
    return round(raw / BYTES_PER_TOKEN)


def read_source(index: int, request: ModelRequest) -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall(f"read-{index}", "read_file", json.dumps({"path": f"part{index}.py"})),),
        stop_reason="tool_calls",
        reasoning_content=f"PRIVATE-thinking-for-read-{index}",
        usage={"prompt_tokens": prompt_tokens(request), "completion_tokens": 24},
    )


def assert_thinking_groups(request: ModelRequest) -> None:
    claimed = []
    for index, message in enumerate(request.messages):
        if message.tool_calls:
            assert len(message.tool_calls) == 1
            call_id = message.tool_calls[0].call_id
            assert message.reasoning_content == f"PRIVATE-thinking-for-{call_id}"
            tool = request.messages[index + 1]
            assert tool.role == "tool" and tool.tool_call_id == call_id
            claimed.append(call_id)
    assert claimed == [message.tool_call_id for message in request.messages if message.role == "tool"]


def stable_prefix(request: ModelRequest) -> tuple[bytes, ...]:
    messages = request.messages
    # Latest task evidence is a request suffix. The historical prefix ends at the final result.
    while messages and messages[-1].role == "system":
        messages = messages[:-1]
    return tuple(
        json.dumps(serialize_message(message, include_reasoning=True), ensure_ascii=False).encode()
        for message in messages
    )


def first_compaction(events: tuple[AgentEvent, ...]) -> tuple[int, AgentEvent]:
    event = next(event for event in events if event.type == "context.compacted")
    start = next(
        item for item in events if item.seq > event.seq and item.type == "model.started"
    )
    return start.payload["request_index"] - 1, event


async def test_large_read_pipeline_calibrates_and_reuses_a_byte_identical_checkpoint(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rounds = 25
    for index in range(rounds):
        detail = "source code " * (5 if index < 9 else 4)
        source = "".join(f"# item {line}: {detail}\n" for line in range(300))
        (workspace / f"part{index}.py").write_text(
            source + f"# FINAL_MARKER_{index}\n", encoding="utf-8"
        )
    prior = (Message("user", "Earlier task"), Message("assistant", "Earlier recorded reply."))
    reports = {}
    for label, calibration, sticky, protect in (
        ("legacy", False, False, False),
        ("calibration_only", True, False, False),
        ("all_enabled", True, True, True),
    ):
        index = 0

        def script(request: ModelRequest) -> AssistantReply:
            nonlocal index
            assert_thinking_groups(request)
            current = index
            index += 1
            if current < rounds:
                return read_source(current, request)
            return AssistantReply(
                "Analysis complete",
                usage={"prompt_tokens": prompt_tokens(request), "completion_tokens": 24},
            )

        model = ThinkingModel(script, context_window_tokens=WINDOW)
        with SessionStore(workspace, tmp_path / label) as store:
            agent = Agent(
                model,
                ToolRegistry(
                    workspace,
                    PolicyEngine("read-only", workspace=workspace),
                    artifacts_dir=store.artifacts_dir,
                ),
                store=store,
                max_output_tokens=OUTPUT,
                enforce_run_budget=False,
                adaptive_round_retention=True,
                harness_config=HarnessConfig(
                    pipeline_metrics=True,
                    smart_tool_truncation=True,
                    task_evidence=True,
                    calibrated_context_budget=calibration,
                    sticky_context_checkpoint=sticky,
                    protect_fresh_tool_output=protect,
                ),
            )
            outcome = await agent.run("Read and analyze the large source files.", history=prior)
            assert outcome.run_status == "completed", outcome.reason
            assert outcome.tool_calls == rounds and outcome.model_calls == rounds + 1
            events = store.load_events()
            first, compacted = first_compaction(events)
            before = compacted.payload.get(
                "before_raw_utf8_bytes", compacted.payload["before_tokens_estimated"]
            )
            after = compacted.payload["after_tokens_estimated"]
            starts = [item.payload for item in events if item.type == "model.started"]
            completed = [item.payload for item in events if item.type == "model.completed"]
            prefix = stable_prefix(model.requests[first])
            stable_count = sum(
                stable_prefix(request)[: len(prefix)] == prefix
                for request in model.requests[first + 1 : first + 4]
            )
            reports[label] = {
                "compactions": sum(item.type == "context.compacted" for item in events),
                "first_true_uncompressed_tokens": round(before / BYTES_PER_TOKEN),
                "first_after_estimate": after,
                "median_estimate_error_percent_after_first_call": round(
                    100 * median(item["input_estimate_error_ratio"] for item in completed[1:]), 2
                ),
                "next_three_stable_prefixes": stable_count,
            }
            assert after <= (WINDOW - OUTPUT - RESERVE) * 0.55
            persisted = store.load_messages()
            originals = {
                item.tool_call_id: item.content for item in agent.messages if item.role == "tool"
            }
            assert len(originals) == rounds
            assert all(item.reasoning_content is None for item in persisted)
            assert [json.loads(item.content) for item in persisted if item.role == "tool"] == [
                json.loads(content) for content in originals.values()
            ]

            if calibration:
                assert 140_000 <= reports[label]["first_true_uncompressed_tokens"] <= 160_000
                assert starts[0]["calibration_factor"] == 1.0
                assert starts[first]["calibration_factor"] == 0.30
                assert starts[first]["raw_input_utf8_bytes"] == compacted.payload[
                    "after_raw_utf8_bytes"
                ]
                assert starts[first]["input_tokens"] == after
                assert all(item["input_estimate_error_ratio"] < 0.09 for item in completed[1:])
            if sticky:
                assert after >= (WINDOW - OUTPUT - RESERVE) * 0.45
                assert stable_count == 3
                next_starts = [
                    event.seq for event in events if event.type == "model.started"
                ][first : first + 4]
                assert not any(
                    event.type == "context.compacted"
                    and next_starts[0] < event.seq < next_starts[-1]
                    for event in events
                )
                # Volatile evidence is still refreshed on each append.
                assert model.requests[first].messages[-1] != model.requests[first + 1].messages[-1]
                tool_messages = [
                    item for item in model.requests[first].messages if item.role == "tool"
                ]
                assert [item.tool_call_id for item in tool_messages[-2:]] == [
                    f"read-{first - 2}", f"read-{first - 1}"
                ]
                assert any(json.loads(item.content).get("projection") for item in tool_messages[:-2])
                for item in tool_messages[-2:]:
                    assert item.content == originals[item.tool_call_id]
                    body = json.loads(item.content)
                    assert len(body["content"]) > 1024 and len(body["data"]["lines"]) == 301
                    assert "FINAL_MARKER" in body["data"]["lines"][-1]["text"]
                assert (await agent.compact())["model_calls"] == 0
                assert sum(item.type == "context.compacted" for item in store.load_events()) == (
                    reports[label]["compactions"] + 1
                )
                assert len(model.requests) == rounds + 1
                assert store.load_messages() == persisted
                await agent.clear_context()
                await agent.continue_run("Start with empty working context.")
                assert not any(item.role == "tool" for item in model.requests[-1].messages)
                assert store.load_messages()[: len(persisted)] == persisted

    assert 40_000 <= reports["legacy"]["first_true_uncompressed_tokens"] <= 50_000
    assert reports["legacy"]["compactions"] > reports["calibration_only"]["compactions"]
    assert reports["calibration_only"]["compactions"] > reports["all_enabled"]["compactions"]
    assert reports["legacy"]["next_three_stable_prefixes"] == 0
    print(json.dumps(reports, indent=2, sort_keys=True))


@pytest.mark.parametrize("fails_again", [False, True])
async def test_server_fallback_can_clip_fresh_output_without_replaying_tools(
    tmp_path: Path, fails_again: bool
) -> None:
    (tmp_path / "part0.py").write_text("# plenty of full source\n" * 350, encoding="utf-8")
    attempts = 0

    def script(request: ModelRequest) -> AssistantReply:
        nonlocal attempts
        attempts += 1
        assert_thinking_groups(request)
        if attempts == 1:
            return read_source(0, request)
        result = next(item for item in request.messages if item.role == "tool")
        body = json.loads(result.content)
        if attempts == 2:
            assert len(body["content"]) > 1024 and "projection" not in body
        else:
            assert body["projection"]["truncated"]
            assert "[... projection truncated ...]" in body["content"]
        if attempts == 2 or fails_again:
            raise ProviderError("Provider context limit", context_length_exceeded=True, status_code=400)
        return AssistantReply("The shorter request worked.")

    model = ThinkingModel(script, context_window_tokens=WINDOW)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = Agent(
            model,
            ToolRegistry(
                tmp_path,
                PolicyEngine("read-only", workspace=tmp_path),
                artifacts_dir=store.artifacts_dir,
            ),
            store=store,
            max_output_tokens=OUTPUT,
            project_context=False,
            harness_config=HarnessConfig(
                calibrated_context_budget=True,
                sticky_context_checkpoint=True,
                protect_fresh_tool_output=True,
                smart_tool_truncation=True,
            ),
        )
        result = await agent.run("Read the file once.")
        events = store.load_events()
        retry, = (event.payload for event in events if event.type == "context.retry")
        compacted, = (event.payload for event in events if event.type == "context.compacted")
        assert compacted["input_budget"] == int(WINDOW * 0.70) - OUTPUT - RESERVE
        assert retry["after_tokens_estimated"] < retry["before_tokens_estimated"]
        assert result.run_status == ("failed" if fails_again else "completed")
        assert attempts == result.model_calls == 3
        assert result.tool_calls == sum(event.type == "tool.started" for event in events) == 1
        assert len([message for message in store.load_messages() if message.role == "tool"]) == 1
