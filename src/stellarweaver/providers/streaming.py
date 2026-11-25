"""Bounded Chat Completions aggregation; partial arguments never become actions."""

from dataclasses import dataclass, field

from openai.types.chat import ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage

from stellarweaver.domain import AssistantReply, JSONDict, ToolCall
from stellarweaver.providers.base import ProviderError


def usage_fields(usage: CompletionUsage) -> JSONDict:
    fields = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    optional = {
        "prompt_cache_hit_tokens": getattr(usage, "prompt_cache_hit_tokens", None),
        "prompt_cache_miss_tokens": getattr(usage, "prompt_cache_miss_tokens", None),
        "reasoning_tokens": getattr(usage.completion_tokens_details, "reasoning_tokens", None),
    }
    server_tools = getattr(usage, "server_tool_use", None)
    if isinstance(server_tools, dict):
        optional["tool_search"] = server_tools.get("tool_search")
    if optional["prompt_cache_hit_tokens"] is None:
        optional["prompt_cache_hit_tokens"] = getattr(
            usage.prompt_tokens_details, "cached_tokens", None
        )
    for name, value in optional.items():
        if type(value) is int and value >= 0:
            fields[name] = value
    return fields


@dataclass
class _CallFragments:
    call_id: str = ""
    names: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)


REPETITION_ERROR_MESSAGE = "Detected repetitive generation loop in stream"


def _has_reasoning_repetition(
    text: str, *, min_chars: int = 1200, min_repeats: int = 16
) -> bool:
    """Conservatively detect degenerate tail repetition in reasoning_content only."""
    if len(text) < min_chars:
        return False
    window = text[-4000:]
    # 1. Exact consecutive short phrase/n-gram loop at the tail (2..80 chars, >= min_repeats).
    max_period = min(80, len(window) // min_repeats)
    for period in range(2, max_period + 1):
        unit = window[-period:]
        if not unit.strip():
            continue
        span = period * min_repeats
        if window.endswith(unit * min_repeats) and len(window) >= span:
            return True
    # 2. Exact consecutive identical short non-empty lines at the tail (>= min_repeats).
    lines = [line.strip() for line in window.splitlines() if line.strip()]
    if len(lines) >= min_repeats:
        tail = lines[-min_repeats:]
        first = tail[0]
        if 2 <= len(first) <= 120 and all(line == first for line in tail):
            return True
    return False


class ChatCompletionAccumulator:
    """Accept one assistant choice and return a reply only after its finish marker.

    IDs are stable metadata; function names and arguments are ordered fragments.
    JSON/schema validation belongs to dispatch after this entire response commits.
    """

    def __init__(
        self,
        *,
        max_output_tokens: int,
        max_bytes: int | None = None,
        capture_reasoning: bool = False,
        detect_repetition: bool = False,
        repetition_min_chars: int = 1200,
        repetition_min_repeats: int = 16,
    ) -> None:
        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else min(16 * 1024 * 1024, max(8192, max_output_tokens * 32))
        )
        if self.max_bytes < 1:
            raise ValueError("Stream byte limit must be positive")
        self._bytes = 0
        self._text: list[str] = []
        self._capture_reasoning = capture_reasoning
        self._reasoning: list[str] = []
        self._detect_repetition = detect_repetition
        self._repetition_min_chars = repetition_min_chars
        self._repetition_min_repeats = repetition_min_repeats
        self._reasoning_chars = 0
        self._next_repetition_check = repetition_min_chars
        self._calls: dict[int, _CallFragments] = {}
        self._ids: dict[str, int] = {}
        self._stop_reason: str | None = None
        self.usage: JSONDict | None = None
        self._provider_model: str | None = None
        self._provider_fingerprint: str | None = None
        self.partial = False

    def partial_output_tokens(self) -> int:
        if self._bytes <= 0:
            return 0
        return max(1, (self._bytes + 3) // 4)

    def _fail(self, message: str) -> ProviderError:
        return ProviderError(
            message,
            usage=self.usage,
            partial_stream=self.partial,
            partial_output_tokens=self.partial_output_tokens(),
        )

    def _count(self, value: str) -> None:
        self._bytes += len(value.encode("utf-8"))
        if self._bytes > self.max_bytes:
            raise self._fail("Model stream exceeded the bounded response buffer")

    def feed(self, chunk: ChatCompletionChunk) -> str:
        """Consume an SDK-decoded chunk and return only displayable text."""
        if chunk.usage is not None:
            self.usage = usage_fields(chunk.usage)
        self._provider_model = chunk.model or self._provider_model
        self._provider_fingerprint = chunk.system_fingerprint or self._provider_fingerprint
        if not chunk.choices:
            return ""
        if len(chunk.choices) != 1 or chunk.choices[0].index != 0:
            raise self._fail("Expected exactly one streamed assistant choice")
        if self._stop_reason is not None:
            raise self._fail("Model stream contained a choice after its finish marker")
        choice = chunk.choices[0]
        delta = choice.delta
        if delta.role not in (None, "assistant"):
            raise self._fail("Model stream returned a non-assistant role")
        if delta.function_call is not None:
            raise self._fail("Model stream used unsupported legacy function calls")
        if self._capture_reasoning:
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning is not None:
                if not isinstance(reasoning, str):
                    raise self._fail("DeepSeek returned an invalid reasoning field")
                self._count(reasoning)
                self._reasoning.append(reasoning)
                self.partial |= bool(reasoning)
                if self._detect_repetition and reasoning:
                    self._reasoning_chars += len(reasoning)
                    if self._reasoning_chars >= self._next_repetition_check:
                        self._next_repetition_check = self._reasoning_chars + 256
                        if _has_reasoning_repetition(
                            "".join(self._reasoning),
                            min_chars=self._repetition_min_chars,
                            min_repeats=self._repetition_min_repeats,
                        ):
                            raise ProviderError(
                                REPETITION_ERROR_MESSAGE,
                                retryable=True,
                                usage=self.usage,
                                partial_stream=True,
                                partial_output_tokens=self.partial_output_tokens(),
                            )
        text = delta.content or ""
        if text:
            self._count(text)
            self._text.append(text)
            self.partial = True
        for call in delta.tool_calls or []:
            if call.index < 0 or call.index > 1023:
                raise self._fail("Model stream returned an invalid tool call index")
            if call.type not in (None, "function"):
                raise self._fail("Model stream returned an unsupported tool call type")
            if call.index not in self._calls:
                if len(self._calls) >= 128:
                    raise self._fail("Model stream exceeded the tool call count limit")
                self._calls[call.index] = _CallFragments()
            fragments = self._calls[call.index]
            self.partial = True
            if call.id is not None:
                if not call.id or (fragments.call_id and fragments.call_id != call.id):
                    raise self._fail("Model stream changed a tool call ID")
                if call.id in self._ids and self._ids[call.id] != call.index:
                    raise self._fail("Model stream returned duplicate tool call IDs")
                if not fragments.call_id:
                    self._count(call.id)
                    fragments.call_id = call.id
                    self._ids[call.id] = call.index
            if call.function is not None:
                if call.function.name:
                    self._count(call.function.name)
                    fragments.names.append(call.function.name)
                if call.function.arguments:
                    self._count(call.function.arguments)
                    fragments.arguments.append(call.function.arguments)
        if choice.finish_reason is not None:
            self._stop_reason = choice.finish_reason
        return text

    def finish(self) -> AssistantReply:
        if self._stop_reason is None:
            raise ProviderError(
                "Model stream ended before a finish marker; no response was committed",
                retryable=True,
                usage=self.usage,
                partial_stream=self.partial,
                partial_output_tokens=self.partial_output_tokens(),
            )
        calls = tuple(
            ToolCall(fragments.call_id, "".join(fragments.names), "".join(fragments.arguments))
            for _, fragments in sorted(self._calls.items())
        )
        if self._stop_reason not in {"length", "max_tokens"}:
            if any(not call.call_id or not call.name for call in calls):
                raise self._fail("Model stream ended with incomplete tool call metadata")
            if calls and self._stop_reason != "tool_calls":
                raise self._fail(
                    "Model stream tool calls have no matching tool_calls finish reason"
                )
            if not calls and self._stop_reason == "tool_calls":
                raise self._fail("Model stream finished tool calls without any calls")
        if (
            self._capture_reasoning
            and not self._reasoning
            and self._stop_reason in {"stop", "tool_calls", "end_turn"}
        ):
            raise self._fail(
                "DeepSeek thinking response omitted the reasoning field required for tool conversations"
            )
        return AssistantReply(
            text="".join(self._text),
            tool_calls=calls,
            stop_reason=self._stop_reason,
            usage=self.usage,
            reasoning_content="".join(self._reasoning) if self._reasoning else None,
            provider_model=self._provider_model,
            provider_fingerprint=self._provider_fingerprint,
        )
