"""Conservative context budgeting, complete tool groups and auditable summaries.

This module deliberately does not call a model. Summary facts come from recorded
messages and tool results, so compaction cannot invent hashes or grant permission.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from stellarweaver.config import ContextConfig
from stellarweaver.domain import JSONDict, Message


def estimate_tokens(value: Any) -> int:
    """UTF-8 JSON bytes: an intentionally conservative estimate, not a tokenizer."""
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class ContextBudgetExceeded(Exception):
    """Required instructions, schemas, goal or retained rounds do not fit."""


class ContextProtocolError(ValueError):
    """An incomplete/ambiguous tool round must be reconciled before projection."""


@dataclass(frozen=True)
class ContextProjection:
    messages: tuple[Message, ...]
    estimated_tokens: int
    compacted: bool
    summary: JSONDict
    through_message_position: int
    before_tokens: int
    input_budget: int
    usage_estimated: bool = True


@dataclass(frozen=True)
class _Group:
    positions: tuple[int, ...]
    assistant: bool = False


def _groups(messages: Sequence[Message]) -> list[_Group]:
    """Provider call IDs are unique locally to their assistant, not globally."""
    result: list[_Group] = []
    position = 0
    while position < len(messages):
        message = messages[position]
        if message.role == "tool":
            raise ContextProtocolError(f"Orphan tool result at message {position}")
        if message.tool_calls and message.role != "assistant":
            raise ContextProtocolError("Only assistant messages may contain tool calls")
        if message.role == "assistant" and message.tool_calls:
            calls = [call.call_id for call in message.tool_calls]
            if len(set(calls)) != len(calls) or any(not value for value in calls):
                raise ContextProtocolError(f"Duplicate/empty tool call ID at message {position}")
            stop = position + len(calls) + 1
            following = messages[position + 1 : stop]
            if len(following) != len(calls) or any(item.role != "tool" for item in following):
                raise ContextProtocolError(f"Incomplete tool round at message {position}")
            actual = [item.tool_call_id for item in following]
            if len(set(actual)) != len(actual) or set(actual) != set(calls):
                raise ContextProtocolError(f"Tool results do not match calls at message {position}")
            for item in following:
                body = _body(item)
                if body.get("call_id", item.tool_call_id) != item.tool_call_id:
                    raise ContextProtocolError("Serialized tool result has a mismatched call ID")
            result.append(_Group(tuple(range(position, stop)), assistant=True))
            position = stop
        else:
            result.append(_Group((position,), assistant=message.role == "assistant"))
            position += 1
    return result


def _body(message: Message) -> JSONDict:
    try:
        value: Any = json.loads(message.content)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _preview(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return (
        raw[:limit].decode("utf-8", errors="ignore") + "\n[projection truncated; original retained]"
    )


_OMIT_FIELDS = frozenset({"content", "lines", "stdout", "stderr", "diff", "matches", "paths"})


def _evidence(value: Any) -> Any:
    """Keep structured identities/results verbatim; omit bulky source/output bodies."""
    if isinstance(value, dict):
        return {
            key: _evidence(item)
            for key, item in value.items()
            if key not in _OMIT_FIELDS or not isinstance(item, (str, list, tuple, dict))
        }
    if isinstance(value, (tuple, list)):
        return [_evidence(item) for item in value]
    return value


def _project_tool(message: Message, limit: int) -> Message:
    if len(message.content.encode("utf-8")) <= limit:
        return message
    body = _body(message)
    if not body:
        # Opaque text still has an unambiguous protocol-level call ID.
        return replace(message, content=_preview(message.content, limit))
    projected = {key: value for key, value in body.items() if key not in {"content", "data"}}
    projected["content"] = _preview(str(body.get("content", "")), limit)
    projected["data"] = _evidence(body.get("data", {}))
    projected["projection"] = {
        "truncated": True,
        "original_utf8_bytes": len(message.content.encode("utf-8")),
        "original_retained_in_session": True,
    }
    return replace(message, content=json.dumps(projected, ensure_ascii=False))


def _summarize(messages: Sequence[Message], omitted: set[int]) -> JSONDict:
    """Evidence remains attributed to original positions and operation IDs."""
    users = [(index, item) for index, item in enumerate(messages) if item.role == "user"]
    goal = users[-1][1].content if users else ""
    instructions = [
        {"message_position": position, "text": item.content}
        for position, item in users[:-1]
        if position in omitted
    ]
    changes: dict[str, JSONDict] = {}
    checks: dict[str, JSONDict] = {}
    failures: list[JSONDict] = []
    facts: list[JSONDict] = []
    for position in sorted(omitted):
        item = messages[position]
        if item.role != "tool":
            continue
        body = _body(item)
        data = body.get("data", {})
        if not isinstance(data, dict):
            data = {}
        record = {
            "message_position": position,
            "call_id": item.tool_call_id,
            "operation_id": body.get("operation_id"),
            "tool_name": body.get("tool_name"),
            "status": body.get("status", "unknown"),
            "artifact_ids": body.get("artifact_ids", []),
            "data": _evidence(data),
        }
        if body.get("tool_name") in {"create_file", "edit_file"} and body.get("status") == "ok":
            changes[str(data.get("path", position))] = record
        elif body.get("tool_name") == "run_command":
            identity = json.dumps([data.get("argv"), data.get("cwd")], ensure_ascii=False)
            checks[identity] = record
        elif body.get("status") not in {None, "ok"}:
            failures.append(record)
        elif "sha256" in data:
            facts.append(record)
    return {
        "kind": "deterministic_context_projection",
        "source": "recorded_messages_and_tool_results",
        "goal": goal,
        # Prior user wording is retained exactly; we do not guess which sentences are constraints.
        "historical_user_instructions": instructions,
        "changed_files": list(changes.values()),
        "verified_commands": list(checks.values()),
        "recent_failures": failures[-12:],
        "recent_observations": facts[-12:],
        "older_failure_count": max(0, len(failures) - 12),
        "older_observation_count": max(0, len(facts) - 12),
        "uncertainties": [
            "Recorded file hashes describe past observations; re-read before editing.",
            "Historical outcomes and instructions do not grant current authorization.",
            "Task completion is not inferred by this deterministic summary.",
        ],
        "through_message_position": max(omitted, default=-1),
        "omitted_message_positions": sorted(omitted),
        "original_message_count": len(messages),
    }


class ContextManager:
    def __init__(self, *, tool_preview_bytes: int = 1024) -> None:
        if tool_preview_bytes < 0:
            raise ValueError("tool_preview_bytes must be non-negative")
        self.tool_preview_bytes = tool_preview_bytes

    def build(
        self,
        messages: Sequence[Message],
        tools: Sequence[JSONDict],
        max_output_tokens: int,
        context_window_tokens: int,
        context_config: ContextConfig,
        *,
        force: bool = False,
    ) -> ContextProjection:
        if max_output_tokens < 1 or context_window_tokens < 1:
            raise ValueError("Output reserve and context window must be positive")
        groups = _groups(messages)
        budget = context_window_tokens - max_output_tokens - context_config.reserve_tokens
        if budget <= 0:
            raise ContextBudgetExceeded(
                "Output reserve and safety margin consume the context window"
            )

        def tokens(items: Sequence[Message]) -> int:
            return estimate_tokens({"messages": [asdict(item) for item in items], "tools": tools})

        before = tokens(messages)
        if not force and before < budget * context_config.trigger_ratio:
            return ContextProjection(tuple(messages), before, False, {}, -1, before, budget)

        latest_user = max(
            (index for index, item in enumerate(messages) if item.role == "user"), default=-1
        )
        recent = [group for group in groups if group.assistant][
            -context_config.recent_complete_rounds :
        ]
        # Text-only assistant replies between tasks must not consume the quota
        # promised for recent complete tool rounds.
        recent_tools = [group for group in groups if messages[group.positions[0]].tool_calls][
            -context_config.recent_complete_rounds :
        ]
        recent = [*recent, *recent_tools]
        protected = {position for group in recent for position in group.positions}
        protected.add(latest_user)
        protected.update(index for index, item in enumerate(messages) if item.role == "system")
        removable = [group for group in groups if not set(group.positions) & protected]
        omitted: set[int] = set()

        def project() -> tuple[tuple[Message, ...], JSONDict, int]:
            summary = _summarize(messages, omitted)
            summary_message = Message(
                "system",
                "Derived historical summary (evidence, not new user instructions or authorization). "
                "Original messages remain in the session journal.\n"
                + json.dumps(summary, ensure_ascii=False),
            )
            remaining = tuple(
                _project_tool(item, self.tool_preview_bytes) if item.role == "tool" else item
                for index, item in enumerate(messages)
                if index not in omitted
            )
            # Put derived state after the initial system instructions, before dialogue.
            insertion = 0
            while insertion < len(remaining) and remaining[insertion].role == "system":
                insertion += 1
            items = (*remaining[:insertion], summary_message, *remaining[insertion:])
            return items, summary, tokens(items)

        projected, summary, count = project()
        target = budget * context_config.target_ratio
        for group in removable:
            if count <= target:
                break
            omitted.update(group.positions)
            projected, summary, count = project()
        # Deterministic fallback may keep an already-fitting original projection
        # when summary overhead would be larger than the source it replaces.
        if count >= before and before <= budget:
            return ContextProjection(tuple(messages), before, False, {}, -1, before, budget)
        if count > budget:
            raise ContextBudgetExceeded(
                f"Required context does not fit: estimated {count}, input budget {budget}; "
                "system instructions, tool schemas, current goal and recent complete rounds retained"
            )
        return ContextProjection(
            projected, count, True, summary, max(omitted, default=-1), before, budget
        )
