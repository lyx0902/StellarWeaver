"""Chat Completions adapter, verified with real SDK transport fixtures."""

import math
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, omit
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionToolUnionParam

from stellarweaver.config import ModelConfig
from stellarweaver.domain import (
    AssistantReply,
    JSONDict,
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
    ToolCall,
)
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.streaming import ChatCompletionAccumulator


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse seconds or HTTP-date; oversized valid delays remain too large to wait.

    The runtime compares the full delay with its remaining wall budget. In particular,
    a huge numeric header must not be silently reduced into an early retry.
    """
    if value is None:
        return None
    value = value.strip()
    if len(value) > 256:
        return math.inf if value.isascii() and value.isdecimal() else None
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0.0, (date - (now or datetime.now(UTC))).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


_CONTEXT_LENGTH_CODES = frozenset(
    {
        "context_length_exceeded",
        "context_window_exceeded",
        "max_context_length_exceeded",
        "maximum_context_length_exceeded",
        "context_length_limit_exceeded",
    }
)


def _context_length_failure(exc: APIStatusError) -> bool:
    """Inspect known vendor signals, returning only a sanitized boolean.

    HTTP 413 alone describes a payload limit and must not trigger model-context
    compaction. A generic HTTP 400 likewise remains a non-retryable config error.
    """
    if exc.status_code not in {400, 413}:
        return False
    body = exc.body
    if not isinstance(body, dict):
        return False
    error = body.get("error", body)
    if not isinstance(error, dict):
        return False
    for key in ("code", "type"):
        code = error.get(key)
        if isinstance(code, str) and code.casefold() in _CONTEXT_LENGTH_CODES:
            return True
    message = error.get("message")
    if not isinstance(message, str):
        return False
    # Bound examination of an untrusted error body; no vendor text is emitted.
    text = message[:8192].casefold()
    if any(text == code or text.startswith(code + ":") for code in _CONTEXT_LENGTH_CODES):
        return True
    marker = r"(?:maximum\s+)?context\s+(?:length|window)"
    return bool(
        re.search(marker + r"\s+(?:(?:has\s+been|is|was)\s+)?exceed(?:s|ed)?\b", text)
        or re.search(r"\bexceed(?:s|ed)?\s+(?:(?:the|model's|maximum)\s+)*" + marker, text)
        or re.search(r"too\s+(?:long|large)\s+for\s+(?:(?:the|model's)\s+)*" + marker, text)
        or re.search(
            r"maximum\s+context\s+length.{0,300}(?:however.{0,100}requested|messages\s+resulted)",
            text,
            re.DOTALL,
        )
    )


def _provider_failure(
    exc: Exception, *, usage: JSONDict | None = None, partial_stream: bool = False
) -> ProviderError:
    if isinstance(exc, (APITimeoutError, httpx.TimeoutException)):
        message = "Model request timed out; no response was committed"
        retryable = True
    elif isinstance(exc, (APIConnectionError, httpx.TransportError)):
        message = "Model connection failed; no response was committed"
        retryable = True
    elif isinstance(exc, APIStatusError):
        context_length_exceeded = _context_length_failure(exc)
        return ProviderError(
            (
                f"Model API returned HTTP {exc.status_code}; model context length exceeded"
                if context_length_exceeded
                else f"Model API returned HTTP {exc.status_code}; check provider configuration"
            ),
            retryable=exc.status_code in (408, 409, 429) or 500 <= exc.status_code <= 599,
            retry_after_seconds=parse_retry_after(exc.response.headers.get("retry-after")),
            status_code=exc.status_code,
            usage=usage,
            partial_stream=partial_stream,
            context_length_exceeded=context_length_exceeded,
        )
    else:
        # SDK exception strings/vendor bodies may contain credentials or raw input.
        message = "Model protocol failed; no response was committed"
        retryable = False
    return ProviderError(message, retryable=retryable, usage=usage, partial_stream=partial_stream)


def serialize_message(message: Message) -> JSONDict:
    value: JSONDict = {"role": message.role, "content": message.content}
    if message.tool_calls:
        value["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in message.tool_calls
        ]
    if message.role == "tool":
        if not message.tool_call_id:
            raise ProviderError("Tool result is missing its tool_call_id")
        value["tool_call_id"] = message.tool_call_id
    return value


class OpenAICompatibleChatProvider:
    """One request per complete call; SDK retries disabled for visible budget accounting."""

    def __init__(
        self, config: ModelConfig, api_key: str, *, client: AsyncOpenAI | None = None
    ) -> None:
        if not api_key.strip():
            raise ValueError("An API key is required for the real model provider")
        self.config = config
        self._owns_client = client is None
        self._client = (
            client.with_options(max_retries=0)
            if client is not None
            else AsyncOpenAI(
                api_key=api_key,
                base_url=config.base_url,
                timeout=config.request_timeout_seconds,
                max_retries=0,
            )
        )

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            tool_calling=self.config.supports_tools,
            streaming=self.config.supports_streaming,
            parallel_tool_calls=False,
            context_window_tokens=self.config.context_window_tokens,
        )

    async def complete(self, request: ModelRequest) -> AssistantReply:
        if request.tools and not self.config.supports_tools:
            raise ProviderError("Configured provider does not support function tools")
        extra_body = (
            {"thinking": {"type": "disabled"}} if self.config.provider == "deepseek" else None
        )
        try:
            response = await self._client.chat.completions.create(
                model=self.config.model,
                messages=cast(
                    list[ChatCompletionMessageParam],
                    [serialize_message(message) for message in request.messages],
                ),
                tools=(
                    cast(list[ChatCompletionToolUnionParam], list(request.tools))
                    if request.tools
                    else omit
                ),
                max_tokens=min(request.max_output_tokens, self.config.max_output_tokens),
                stream=False,
                extra_body=extra_body,
            )
        except (APIError, httpx.HTTPError) as exc:
            raise _provider_failure(exc) from exc
        if len(response.choices) != 1:
            raise ProviderError("Expected exactly one complete assistant choice")
        choice = response.choices[0]
        if choice.finish_reason is None:
            raise ProviderError("Model response did not include a complete stop reason")
        calls: list[ToolCall] = []
        seen: set[str] = set()
        for call in choice.message.tool_calls or []:
            if call.type != "function":
                raise ProviderError("Provider returned an unsupported tool call type")
            if not call.id or not call.function.name or call.id in seen:
                raise ProviderError("Provider returned an invalid or duplicate tool call ID")
            seen.add(call.id)
            calls.append(ToolCall(call.id, call.function.name, call.function.arguments))
        usage: JSONDict | None = None
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        # Deliberately do not read or re-transmit vendor hidden reasoning fields.
        return AssistantReply(
            text=choice.message.content or "",
            tool_calls=tuple(calls),
            stop_reason=choice.finish_reason,
            usage=usage,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        if not self.config.supports_streaming:
            # Explicitly configured fallback produces one complete response, with no
            # intermediate arguments or pretend streamed network request.
            reply = await self.complete(request)
            yield ModelStreamEvent("response.completed", reply=reply)
            return
        if request.tools and not self.config.supports_tools:
            raise ProviderError("Configured provider does not support function tools")
        accumulator = ChatCompletionAccumulator(
            max_output_tokens=min(request.max_output_tokens, self.config.max_output_tokens)
        )
        extra_body = (
            {"thinking": {"type": "disabled"}} if self.config.provider == "deepseek" else None
        )
        try:
            response = await self._client.chat.completions.create(
                model=self.config.model,
                messages=cast(
                    list[ChatCompletionMessageParam],
                    [serialize_message(message) for message in request.messages],
                ),
                tools=(
                    cast(list[ChatCompletionToolUnionParam], list(request.tools))
                    if request.tools
                    else omit
                ),
                max_tokens=min(request.max_output_tokens, self.config.max_output_tokens),
                stream=True,
                stream_options={"include_usage": True},
                extra_body=extra_body,
            )
            async with response:
                async for chunk in response:
                    text = accumulator.feed(chunk)
                    if text:
                        yield ModelStreamEvent("text.delta", text=text)
            reply = accumulator.finish()
        except (APIError, httpx.HTTPError) as exc:
            raise _provider_failure(
                exc, usage=accumulator.usage, partial_stream=accumulator.partial
            ) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderError(
                "Model stream returned invalid protocol data; no response was committed",
                usage=accumulator.usage,
                partial_stream=accumulator.partial,
            ) from exc
        yield ModelStreamEvent("response.completed", reply=reply)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()
