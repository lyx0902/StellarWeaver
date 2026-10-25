"""Bounded Chat Completions aggregation; partial arguments never become actions."""

from dataclasses import dataclass, field

from openai.types.chat import ChatCompletionChunk

from stellarweaver.domain import AssistantReply, JSONDict, ToolCall
from stellarweaver.providers.base import ProviderError


@dataclass
class _CallFragments:
    call_id: str = ""
    names: list[str] = field(default_factory=list)
    arguments: list[str] = field(default_factory=list)


class ChatCompletionAccumulator:
    """Accept one assistant choice and return a reply only after its finish marker.

    IDs are stable metadata; function names and arguments are ordered fragments.
    JSON/schema validation belongs to dispatch after this entire response commits.
    """

    def __init__(self, *, max_output_tokens: int, max_bytes: int | None = None) -> None:
        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else min(16 * 1024 * 1024, max(8192, max_output_tokens * 32))
        )
        if self.max_bytes < 1:
            raise ValueError("Stream byte limit must be positive")
        self._bytes = 0
        self._text: list[str] = []
        self._calls: dict[int, _CallFragments] = {}
        self._ids: dict[str, int] = {}
        self._stop_reason: str | None = None
        self.usage: JSONDict | None = None
        self.partial = False

    def _fail(self, message: str) -> ProviderError:
        return ProviderError(message, usage=self.usage, partial_stream=self.partial)

    def _count(self, value: str) -> None:
        self._bytes += len(value.encode("utf-8"))
        if self._bytes > self.max_bytes:
            raise self._fail("Model stream exceeded the bounded response buffer")

    def feed(self, chunk: ChatCompletionChunk) -> str:
        """Consume an SDK-decoded chunk and return only displayable text."""
        if chunk.usage is not None:
            self.usage = {
                "prompt_tokens": chunk.usage.prompt_tokens,
                "completion_tokens": chunk.usage.completion_tokens,
                "total_tokens": chunk.usage.total_tokens,
            }
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
        # Vendor reasoning_content is intentionally never read or retained.
        return text

    def finish(self) -> AssistantReply:
        if self._stop_reason is None:
            raise ProviderError(
                "Model stream ended before a finish marker; no response was committed",
                retryable=True,
                usage=self.usage,
                partial_stream=self.partial,
            )
        calls = tuple(
            ToolCall(fragments.call_id, "".join(fragments.names), "".join(fragments.arguments))
            for _, fragments in sorted(self._calls.items())
        )
        if self._stop_reason != "length":
            if any(not call.call_id or not call.name for call in calls):
                raise self._fail("Model stream ended with incomplete tool call metadata")
            if calls and self._stop_reason != "tool_calls":
                raise self._fail(
                    "Model stream tool calls have no matching tool_calls finish reason"
                )
            if not calls and self._stop_reason == "tool_calls":
                raise self._fail("Model stream finished tool calls without any calls")
        return AssistantReply(
            text="".join(self._text),
            tool_calls=calls,
            stop_reason=self._stop_reason,
            usage=self.usage,
        )
