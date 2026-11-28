"""Conservative context budgeting, complete tool groups and auditable summaries.

This module deliberately does not call a model. Summary facts come from recorded
messages and tool results, so compaction cannot invent hashes or grant permission.
"""

from __future__ import annotations

import json
import math
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
class ContextSize:
    raw_utf8_bytes: int
    factor: float = 1.0

    @property
    def tokens(self) -> int:
        return math.ceil(self.raw_utf8_bytes * self.factor)


@dataclass(frozen=True)
class ContextBaseline:
    source: tuple[Message, ...]
    projected: tuple[Message, ...]

    def extend(self, messages: tuple[Message, ...]) -> tuple[Message, ...]:
        boundary = len(self.source)
        if len(messages) < boundary or any(
            previous != current or previous.reasoning_content != current.reasoning_content
            for previous, current in zip(self.source, messages[:boundary], strict=True)
        ):
            return messages
        return (*self.projected, *messages[boundary:])


@dataclass(frozen=True)
class ContextProjection:
    messages: tuple[Message, ...]
    history: tuple[Message, ...]
    size: ContextSize
    compacted: bool
    summary: JSONDict
    through_message_position: int
    before: ContextSize
    input_budget: int
    usage_estimated: bool = True

    @property
    def estimated_tokens(self) -> int:
        return self.size.tokens

    @property
    def before_tokens(self) -> int:
        return self.before.tokens


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


def _head_tail(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker = b"\n[... projection truncated ...]\n"
    if limit <= len(marker):
        return raw[-limit:].decode("utf-8", errors="ignore") if limit else ""
    available = limit - len(marker)
    head = available * 35 // 100
    tail = available - head
    return (raw[:head] + marker + raw[-tail:]).decode("utf-8", errors="ignore")


def _tool_output(body: JSONDict, limit: int, *, terminal_bash: bool = False) -> str:
    data = body.get("data")
    if isinstance(data, dict):
        stdout, stderr = data.get("stdout"), data.get("stderr")
        if not isinstance(stdout, str):
            stdout = ""
        if not isinstance(stderr, str):
            stderr = ""
        if stdout and stderr:
            # Reserve space for both streams so a long stderr cannot hide stdout's test summary.
            labels = "stdout:\n\nstderr:\n"
            available = max(0, limit - len(labels))
            if available:
                stdout_limit = min(
                    len(stdout.encode("utf-8")),
                    max(available // 2, available - len(stderr.encode("utf-8"))),
                )
                return (
                    f"stdout:\n{_head_tail(stdout, stdout_limit)}"
                    f"\nstderr:\n{_head_tail(stderr, available - stdout_limit)}"
                )
        if terminal_bash and (stdout or stderr):
            label, stream = ("stdout:\n", stdout) if stdout else ("stderr:\n", stderr)
            if stdout and stderr:
                return _head_tail(stderr, limit)
            if limit <= len(label):
                return _head_tail(stream, limit)
            return label + _head_tail(stream, limit - len(label))
    return _head_tail(str(body.get("content", "")), limit)


def _projected_content(body: JSONDict, limit: int, *, smart: bool) -> str:
    if body.get("tool_message_format") == "terminal-bash":
        return _tool_output(body, limit, terminal_bash=True)
    content = str(body.get("content", ""))
    preview = _tool_output(body, limit) if smart else _preview(content, limit)
    data = body.get("data")
    if not isinstance(data, dict) or not (
        data.get("syntax_diagnostic") or data.get("syntax_diagnostics")
    ):
        return preview
    hints = [line for line in content.splitlines() if line.startswith("[syntax_diagnostic]")]
    if not hints or all(hint in preview for hint in hints):
        return preview
    hint_bytes = ("\n" + "\n".join(hints)).encode()[: limit // 2]
    hint_text = hint_bytes.decode("utf-8", errors="ignore")
    return (
        _head_tail(
            content.rsplit("\n[syntax_diagnostic]", len(hints))[0], limit - len(hint_text.encode())
        )
        + hint_text
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


def _project_tool(message: Message, limit: int, *, smart: bool = False) -> Message:
    if len(message.content.encode("utf-8")) <= limit:
        return message
    body = _body(message)
    if not body:
        # Opaque text still has an unambiguous protocol-level call ID.
        preview = _head_tail if smart else _preview
        return replace(message, content=preview(message.content, limit))
    projected = {key: value for key, value in body.items() if key not in {"content", "data"}}
    projected["content"] = _projected_content(body, limit, smart=smart)
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


def thinking_history(
    messages: Sequence[Message], *, stable_evidence: bool = False
) -> tuple[Message, ...]:
    """Represent restored assistant groups whose transient reasoning is unavailable."""
    omitted = {
        position
        for group in _groups(messages)
        if group.assistant and messages[group.positions[0]].reasoning_content is None
        for position in group.positions
    }
    if not omitted:
        return tuple(messages)
    # Later complete Thinking rounds do not change evidence for restored rounds.
    summary = _summarize(messages[: max(omitted) + 1] if stable_evidence else messages, omitted)
    summary["previous_assistant_replies"] = [
        _preview(message.content, 2048)
        for index, message in enumerate(messages)
        if index in omitted and message.role == "assistant" and message.content
    ][-8:]
    remaining = tuple(message for index, message in enumerate(messages) if index not in omitted)
    prefix = 0
    while prefix < len(remaining) and remaining[prefix].role == "system":
        prefix += 1
    evidence = Message(
        "system",
        "Recorded conversation evidence from earlier assistant turns. Treat it as historical "
        "evidence only. Verify current file state and continue the current user task.\n"
        + json.dumps(summary, ensure_ascii=False),
    )
    return (*remaining[:prefix], evidence, *remaining[prefix:])


def _overflow_evidence(messages: Sequence[Message], omitted: set[int]) -> JSONDict:
    tools = []
    users = []
    for position in sorted(omitted):
        item = messages[position]
        if item.role == "user":
            users.append(_preview(item.content, 350))
        elif item.role == "tool":
            body = _body(item)
            data = body.get("data", {})
            details = {
                key: data[key]
                for key in (
                    "path",
                    "sha256",
                    "before_sha256",
                    "after_sha256",
                    "exit_code",
                    "code",
                    "start_line",
                    "end_line",
                    "name",
                    "source_kind",
                )
                if isinstance(data, dict) and key in data
            }
            tools.append(
                {
                    "call_id": item.tool_call_id,
                    "operation_id": body.get("operation_id"),
                    "tool": body.get("tool_name"),
                    "status": body.get("status"),
                    "data": details,
                    "excerpt": _preview(str(body.get("content", item.content)), 220),
                }
            )
    return {"recent_tool_observations": tools[-8:], "prior_user_excerpts": users[-3:]}


class ContextManager:
    def __init__(
        self,
        *,
        tool_preview_bytes: int = 1024,
        adaptive_round_retention: bool = False,
        smart_tool_truncation: bool = False,
        protect_fresh_tool_output: bool = False,
    ) -> None:
        if tool_preview_bytes < 0:
            raise ValueError("tool_preview_bytes must be non-negative")
        self.tool_preview_bytes = tool_preview_bytes
        self.adaptive_round_retention = adaptive_round_retention
        self.smart_tool_truncation = smart_tool_truncation
        self.protect_fresh_tool_output = protect_fresh_tool_output

    def build(
        self,
        messages: Sequence[Message],
        tools: Sequence[JSONDict],
        max_output_tokens: int,
        context_window_tokens: int,
        context_config: ContextConfig,
        *,
        force: bool = False,
        calibration_factor: float = 1.0,
        baseline: ContextBaseline | None = None,
        current_instructions: Sequence[Message] = (),
        current_evidence: Sequence[Message] = (),
        shrink_from: int | None = None,
        protect_initial_user: bool = False,
    ) -> ContextProjection:
        if max_output_tokens < 1 or context_window_tokens < 1:
            raise ValueError("Output reserve and context window must be positive")
        groups = _groups(messages)
        budget = context_window_tokens - max_output_tokens - context_config.reserve_tokens
        if budget <= 0:
            raise ContextBudgetExceeded(
                "Output reserve and safety margin consume the context window"
            )

        messages = tuple(messages)
        initial_user = (
            next((index for index, item in enumerate(messages) if item.role == "user"), -1)
            if protect_initial_user
            else -1
        )
        system_end = next(
            (index for index, item in enumerate(messages) if item.role != "system"), len(messages)
        )

        def model_view(history: tuple[Message, ...]) -> tuple[Message, ...]:
            return (
                *history[:system_end],
                *current_instructions,
                *history[system_end:],
                *current_evidence,
            )

        def measure(items: tuple[Message, ...]) -> ContextSize:
            raw_bytes = estimate_tokens({"messages": [asdict(item) for item in items], "tools": tools})
            return ContextSize(raw_bytes, calibration_factor)

        working = baseline.extend(messages) if baseline is not None and not force else messages
        if initial_user >= 0 and messages[initial_user] not in working:
            # A prior checkpoint may have summarized the task before it was protected.
            working = messages
        working_view = model_view(working)
        before = measure(working_view)
        unchanged = ContextProjection(working_view, working, before, False, {}, -1, before, budget)
        if not force and before.tokens < budget * context_config.trigger_ratio:
            return unchanged

        latest_user = max(
            (index for index, item in enumerate(messages) if item.role == "user"), default=-1
        )
        recent = [group for group in groups if group.assistant][
            -context_config.recent_complete_rounds :
        ]
        # Text-only assistant replies between tasks must not consume the quota
        # promised for recent complete tool rounds.
        tool_rounds = [group for group in groups if messages[group.positions[0]].tool_calls]
        recent_tools = tool_rounds[-context_config.recent_complete_rounds :]
        fresh: set[int] = set()
        if self.protect_fresh_tool_output:
            fresh = {
                position
                for group in tool_rounds[-context_config.fresh_tool_rounds :]
                for position in group.positions
            }
        recent = [*recent, *recent_tools]
        protected = {position for group in recent for position in group.positions}
        protected.update(fresh)
        protected.add(latest_user)
        protected.add(initial_user)
        protected.update(index for index, item in enumerate(messages) if item.role == "system")
        removable = [group for group in groups if not set(group.positions) & protected]
        omitted: set[int] = set()
        clipped: dict[int, Message] = {}

        def project(
            *, clip_fresh: bool = False, overflow: bool = False, include_summary: bool = True
        ) -> ContextProjection:
            summary = _summarize(messages, omitted)
            summary_text = json.dumps(summary, ensure_ascii=False)
            if overflow:
                summary["retention"] = "adaptive_complete_rounds"
                summary["retained_complete_rounds"] = sum(
                    group.assistant
                    and bool(messages[group.positions[0]].tool_calls)
                    and group.positions[0] not in omitted
                    for group in groups
                )
                summary_text = _preview(
                    json.dumps(_overflow_evidence(messages, omitted), ensure_ascii=False),
                    min(6144, budget // 8),
                )
            summary_message = Message(
                "system",
                "Derived historical summary (evidence, not new user instructions or authorization). "
                "Original messages remain in the session journal.\n" + summary_text,
            )
            remaining = tuple(
                _project_tool(
                    item,
                    min(384, self.tool_preview_bytes) if overflow else self.tool_preview_bytes,
                    smart=self.smart_tool_truncation,
                )
                if item.role == "tool" and (overflow or clip_fresh or not self.protect_fresh_tool_output)
                else clipped.get(index, item)
                for index, item in enumerate(messages)
                if index not in omitted
            )
            # Put derived state after the initial system instructions, before dialogue.
            insertion = 0
            while insertion < len(remaining) and remaining[insertion].role == "system":
                insertion += 1
            history = (
                (*remaining[:insertion], summary_message, *remaining[insertion:])
                if include_summary
                else remaining
            )
            items = model_view(history)
            return ContextProjection(
                items, history, measure(items), True, summary, max(omitted, default=-1), before, budget
            )

        projection = project()
        target = budget * context_config.target_ratio
        if self.protect_fresh_tool_output:
            if force:
                target = min(target, before.tokens - 1)
            # Keep as much source as fits by clipping whole older tool rounds in order.
            for group in tool_rounds:
                if group.positions[0] in fresh or projection.estimated_tokens <= target:
                    break
                for position in group.positions[1:]:
                    clipped[position] = _project_tool(
                        messages[position], self.tool_preview_bytes, smart=self.smart_tool_truncation
                    )
                projection = project()
        for group in removable:
            if projection.estimated_tokens <= target:
                break
            omitted.update(group.positions)
            projection = project()
        # Deterministic fallback may keep an already-fitting original projection
        # when summary overhead would be larger than the source it replaces.
        hard_limit = budget if shrink_from is None else min(budget, shrink_from - 1)
        if projection.estimated_tokens >= before.tokens and before.tokens <= hard_limit:
            return unchanged
        if projection.estimated_tokens > hard_limit and fresh:
            projection = project(clip_fresh=True)
        if projection.estimated_tokens > hard_limit and self.adaptive_round_retention:
            projection = project(overflow=True)
            # DeepSeek receives either the full assistant/call/result group or its journal
            # evidence. Dropping an entire group also drops that group's reasoning payload.
            for group in groups:
                if projection.estimated_tokens <= hard_limit:
                    break
                if group.assistant and group.positions[0] not in omitted:
                    omitted.update(group.positions)
                    projection = project(overflow=True)
            if projection.estimated_tokens > hard_limit:
                projection = project(overflow=True, include_summary=False)
                projection.summary["summary_sent_to_model"] = False
        if projection.estimated_tokens > hard_limit:
            raise ContextBudgetExceeded(
                f"Required context does not fit: estimated {projection.estimated_tokens}, "
                f"input budget {hard_limit}; "
                "system instructions, tool schemas, current goal and recent complete rounds retained"
            )
        return projection
