"""Bounded, trusted in-process hooks with a restrictive decision model."""

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Literal, get_args

from stellarweaver.policy.risk import fingerprint

Stage = Literal["PreToolUse", "PermissionRequest", "PostToolUse"]
Decision = Literal["continue", "ask", "deny", "withhold"]
_DECISIONS = {
    "PreToolUse": frozenset({"continue", "ask", "deny"}),
    "PermissionRequest": frozenset({"continue", "deny"}),
    "PostToolUse": frozenset({"continue", "withhold"}),
}


@dataclass(frozen=True)
class HookEvent:
    stage: Stage
    tool: str
    operation_id: str
    invocation_json: str
    result_json: str | None = None


@dataclass(frozen=True)
class TrustedHook:
    identifier: str
    source: str
    version: str
    stage: Stage
    callback: Callable[[HookEvent], Awaitable[Decision]]
    priority: int = 0
    timeout_seconds: float = 1.0
    tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HookReceipt:
    identifier: str
    source: str
    version: str
    outcome: str


class HookManager:
    def __init__(self, hooks: Iterable[TrustedHook] = ()) -> None:
        ordered = tuple(sorted(hooks, key=lambda hook: (hook.priority, hook.identifier)))
        if len({hook.identifier for hook in ordered}) != len(ordered):
            raise ValueError("Trusted hook identifiers must be unique")
        if any(
            not hook.identifier
            or not hook.source
            or not hook.version
            or hook.stage not in get_args(Stage)
            or not math.isfinite(hook.timeout_seconds)
            or hook.timeout_seconds <= 0
            for hook in ordered
        ):
            raise ValueError("A trusted hook requires valid identity, stage and timeout")
        self._hooks = ordered
        self._stages = frozenset(hook.stage for hook in ordered)
        self.binding = fingerprint(
            [
                [
                    hook.identifier,
                    hook.source,
                    hook.version,
                    hook.stage,
                    hook.priority,
                    hook.timeout_seconds,
                    sorted(hook.tools),
                ]
                for hook in ordered
            ]
        )

    def has_stage(self, stage: Stage) -> bool:
        return stage in self._stages

    async def run(self, event: HookEvent) -> tuple[Decision, tuple[HookReceipt, ...]]:
        decision: Decision = "continue"
        records: list[HookReceipt] = []
        for hook in self._hooks:
            if hook.stage != event.stage or (hook.tools and event.tool not in hook.tools):
                continue
            timer = asyncio.timeout(hook.timeout_seconds)
            started = time.monotonic()
            outcome: str
            try:
                async with timer:
                    returned = await hook.callback(event)
                if timer.expired() or time.monotonic() - started > hook.timeout_seconds:
                    raise TimeoutError
                if returned not in _DECISIONS[event.stage]:
                    raise ValueError("Invalid hook decision")
                outcome = returned
            except TimeoutError:
                outcome = "timeout"
            except Exception:
                outcome = "error"
            records.append(HookReceipt(hook.identifier, hook.source, hook.version, outcome))
            if outcome == "deny" or (
                event.stage != "PostToolUse" and outcome in {"timeout", "error"}
            ):
                return "deny", tuple(records)
            if outcome == "ask":
                decision = "ask"
            if outcome == "withhold":
                decision = "withhold"
        return decision, tuple(records)
