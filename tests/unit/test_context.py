"""Context projections preserve protocol, evidence and source history under pressure."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from stellarweaver.config import ContextConfig
from stellarweaver.context import (
    ContextBudgetExceeded,
    ContextManager,
    ContextProtocolError,
    estimate_tokens,
)
from stellarweaver.domain import JSONDict, Message, ToolCall, ToolResult


def round_messages(
    number: int,
    *,
    output_size: int = 8000,
    tool_name: str = "read_file",
    status: str = "ok",
    data: JSONDict | None = None,
) -> list[Message]:
    call = ToolCall("reusable-id", tool_name, '{"path":"source.py"}')
    result = ToolResult(
        call.call_id,
        f"operation-{number}",
        tool_name,
        status,  # type: ignore[arg-type]
        "output " + "x" * output_size,
        data or {"sha256": f"{number:064x}", "path": "source.py", "lines": ["x" * output_size]},
        (f"artifact-{number}",),
    )
    return [
        Message("assistant", tool_calls=(call,)),
        Message("tool", json.dumps(asdict(result)), tool_call_id=call.call_id),
    ]


def test_small_context_is_unchanged_and_counts_schema_output_and_safety() -> None:
    messages = [Message("system", "rules"), Message("user", "修复中文路径")]
    tools = ({"function": {"name": "read_file", "parameters": {"path": "str"}}},)
    projection = ContextManager().build(messages, tools, 1000, 24000, ContextConfig())
    assert projection.messages == tuple(messages)
    assert not projection.compacted
    assert projection.usage_estimated
    assert projection.input_budget == 24000 - 1000 - 2048
    assert projection.estimated_tokens == estimate_tokens(
        {"messages": [asdict(item) for item in messages], "tools": tools}
    )
    assert projection.estimated_tokens > estimate_tokens([asdict(item) for item in messages])


def test_many_rounds_compact_whole_groups_without_mutating_history() -> None:
    messages = [Message("system", "Keep original policy"), Message("user", "fix the source")]
    for index in range(30):
        messages.extend(round_messages(index))
    original = tuple(messages)
    config = ContextConfig(reserve_tokens=500, recent_complete_rounds=2)
    projection = ContextManager().build(messages, (), 1000, 24000, config)
    assert projection.compacted
    assert projection.estimated_tokens < projection.before_tokens
    assert projection.estimated_tokens <= projection.input_budget
    assert tuple(messages) == original
    assert messages[0] in projection.messages and messages[1] in projection.messages
    for item in projection.messages:
        if item.role == "assistant" and item.tool_calls:
            position = projection.messages.index(item)
            assert projection.messages[position + 1].tool_call_id == item.tool_calls[0].call_id
    retained_operations = {
        json.loads(item.content)["operation_id"]
        for item in projection.messages
        if item.role == "tool"
    }
    assert {"operation-28", "operation-29"} <= retained_operations
    summaries = [
        item for item in projection.messages if "Derived historical summary" in item.content
    ]
    assert len(summaries) == 1 and summaries[0].role == "system"
    assert projection.through_message_position == projection.summary["through_message_position"]


def test_call_ids_can_repeat_in_different_rounds() -> None:
    messages = [Message("user", "read")]
    messages.extend(round_messages(1, output_size=1))
    messages.extend(round_messages(2, output_size=1))
    projection = ContextManager().build(messages, (), 100, 20000, ContextConfig(reserve_tokens=0))
    assert projection.messages == tuple(messages)


@pytest.mark.parametrize(
    "messages",
    [
        [Message("tool", "result", tool_call_id="x")],
        [Message("assistant", tool_calls=(ToolCall("x", "read_file", "{}"),))],
        [
            Message("assistant", tool_calls=(ToolCall("x", "read_file", "{}"),)),
            Message("tool", "result", tool_call_id="different"),
        ],
        [
            Message(
                "assistant",
                tool_calls=(ToolCall("x", "read_file", "{}"), ToolCall("x", "read_file", "{}")),
            ),
            Message("tool", "result", tool_call_id="x"),
            Message("tool", "result", tool_call_id="x"),
        ],
        [
            Message("assistant", tool_calls=(ToolCall("x", "read_file", "{}"),)),
            Message("tool", '{"call_id":"wrong"}', tool_call_id="x"),
        ],
        [Message("user", tool_calls=(ToolCall("x", "read_file", "{}"),))],
    ],
)
def test_partial_or_ambiguous_round_is_rejected_even_below_threshold(
    messages: list[Message],
) -> None:
    with pytest.raises(ContextProtocolError):
        ContextManager().build(messages, (), 100, 20000, ContextConfig(reserve_tokens=0))


def test_multicall_round_results_may_arrive_in_different_order_but_never_partially() -> None:
    calls = (ToolCall("a", "read_file", "{}"), ToolCall("b", "read_file", "{}"))
    messages = [
        Message("assistant", tool_calls=calls),
        Message("tool", "B", tool_call_id="b"),
        Message("tool", "A", tool_call_id="a"),
    ]
    projection = ContextManager().build(messages, (), 100, 20000, ContextConfig(reserve_tokens=0))
    assert projection.messages == tuple(messages)
    with pytest.raises(ContextProtocolError):
        ContextManager().build(messages[:-1], (), 100, 20000, ContextConfig(reserve_tokens=0))


def test_large_tool_projection_keeps_error_identity_hash_and_artifacts() -> None:
    data = {
        "path": "中文.py",
        "code": "FILE_CHANGED",
        "details": {"expected_sha256": "a" * 64, "actual_sha256": "b" * 64, "matches": 2},
        "stdout": "x" * 20000,
        "argv": ["python", "-m", "pytest"],
        "cwd": "workspace",
        "exit_code": 1,
        "artifacts": [{"artifact_id": "output-artifact", "sha256": "c" * 64}],
    }
    messages = [Message("system", "rules"), Message("user", "repair")]
    messages.extend(round_messages(1, output_size=20000, status="error", data=data))
    projection = ContextManager(tool_preview_bytes=200).build(
        messages, (), 1000, 12000, ContextConfig(reserve_tokens=500, recent_complete_rounds=1)
    )
    body = json.loads(next(item.content for item in projection.messages if item.role == "tool"))
    assert body["status"] == "error"
    assert body["call_id"] == "reusable-id"
    assert body["operation_id"] == "operation-1"
    assert body["artifact_ids"] == ["artifact-1"]
    assert body["data"]["details"]["actual_sha256"] == "b" * 64
    assert body["data"]["details"]["matches"] == 2
    assert body["data"]["exit_code"] == 1
    assert body["data"]["artifacts"][0]["artifact_id"] == "output-artifact"
    assert "stdout" not in body["data"]
    assert body["projection"]["truncated"]


def test_compaction_extracts_real_command_and_file_evidence_and_prior_user_constraints() -> None:
    messages = [Message("system", "rules"), Message("user", "不要修改 public API，保留中文说明。")]
    messages.extend(
        round_messages(1, tool_name="edit_file", data={"path": "a.py", "after_sha256": "f" * 64})
    )
    messages.extend(
        round_messages(
            2,
            tool_name="run_command",
            status="error",
            data={"argv": ["python", "-m", "pytest"], "cwd": ".", "exit_code": 1},
        )
    )
    messages.append(Message("user", "继续修复失败"))
    messages.extend(round_messages(3))
    projection = ContextManager().build(
        messages, (), 1000, 10000, ContextConfig(reserve_tokens=500, recent_complete_rounds=1)
    )
    assert projection.summary["goal"] == "继续修复失败"
    assert projection.summary["historical_user_instructions"][0]["text"] == messages[1].content
    assert projection.summary["changed_files"][0]["data"]["after_sha256"] == "f" * 64
    assert projection.summary["verified_commands"][0]["data"]["exit_code"] == 1
    assert projection.summary["verified_commands"][0]["status"] == "error"
    assert projection.summary["verified_commands"][0]["artifact_ids"] == ["artifact-2"]


def test_schema_cost_cannot_be_compacted_away() -> None:
    with pytest.raises(ContextBudgetExceeded, match="tool schemas"):
        ContextManager().build(
            [Message("system", "rules"), Message("user", "task")],
            ({"description": "x" * 12000},),
            1000,
            6000,
            ContextConfig(reserve_tokens=500),
        )


@pytest.mark.parametrize("role", ["system", "user"])
def test_mandatory_system_or_current_goal_cannot_be_removed(role: str) -> None:
    messages = [Message(role, "x" * 20000)]  # type: ignore[arg-type]
    with pytest.raises(ContextBudgetExceeded, match="current goal"):
        ContextManager().build(messages, (), 1000, 10000, ContextConfig(reserve_tokens=500))


def test_recent_n_is_not_a_guarantee_that_rounds_fit() -> None:
    messages = [Message("user", "preserve goal")]
    for index in range(6):
        call = ToolCall(str(index), "edit_file", json.dumps({"old_text": "x" * 10000}))
        messages.extend(
            [
                Message("assistant", tool_calls=(call,)),
                Message("tool", "ok", tool_call_id=call.call_id),
            ]
        )
    with pytest.raises(ContextBudgetExceeded, match="recent complete rounds"):
        ContextManager().build(messages, (), 1000, 12000, ContextConfig(reserve_tokens=500))


def test_text_only_replies_do_not_consume_recent_tool_round_quota() -> None:
    messages = [Message("user", "repair")]
    for index in range(10):
        messages.extend(round_messages(index))
        messages.append(Message("assistant", f"observed round {index}"))
    projection = ContextManager().build(
        messages, (), 1000, 24000, ContextConfig(reserve_tokens=500, recent_complete_rounds=3)
    )
    operations = {
        json.loads(item.content)["operation_id"]
        for item in projection.messages
        if item.role == "tool"
    }
    assert {"operation-7", "operation-8", "operation-9"} <= operations


def test_deterministic_fallback_does_not_enlarge_a_fitting_small_context() -> None:
    messages = [Message("system", "rules"), Message("user", "small")]
    projection = ContextManager().build(
        messages, (), 1000, 12000, ContextConfig(reserve_tokens=500), force=True
    )
    assert projection.messages == tuple(messages)
    assert not projection.compacted


def test_output_and_safety_margin_consuming_window_is_explicit() -> None:
    with pytest.raises(ContextBudgetExceeded, match="safety"):
        ContextManager().build([], (), 1000, 1200, ContextConfig(reserve_tokens=500))
