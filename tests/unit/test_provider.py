import json
import math
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from stellarweaver.config import ModelConfig
from stellarweaver.domain import AssistantReply, Message, ModelRequest, ToolCall
from stellarweaver.providers.base import ModelProvider, ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import (
    OpenAICompatibleChatProvider,
    parse_retry_after,
)


async def test_fake_sequence_records_complete_requests() -> None:
    request = ModelRequest((Message("user", "test"),))
    fake = FakeModel([AssistantReply("one"), AssistantReply("two")])
    assert isinstance(fake, ModelProvider)
    assert (await fake.complete(request)).text == "one"
    assert (await fake.complete(request)).text == "two"
    assert fake.requests == [request, request]
    with pytest.raises(ProviderError, match="exhausted"):
        await fake.complete(request)


async def test_fake_callback_and_script(tmp_path: Path) -> None:
    async def callback(request: ModelRequest) -> AssistantReply:
        return AssistantReply(request.messages[-1].content)

    fake = FakeModel(callback=callback)
    assert (await fake.complete(ModelRequest((Message("user", "echo"),)))).text == "echo"
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"text": "offline", "tool_calls": []}]))
    assert (await FakeModel.from_script(script).complete(ModelRequest(()))).text == "offline"


def completion(*, reason: str = "tool_calls", usage: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "response-1",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": reason,
                "message": {
                    "role": "assistant",
                    "content": "Visible text",
                    "reasoning_content": "hidden reasoning must never be persisted",
                    "tool_calls": [
                        {
                            "id": "call-a",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"中文.py"}'},
                        },
                        {
                            "id": "call-b",
                            "type": "function",
                            "function": {"name": "git_status", "arguments": "{}"},
                        },
                    ],
                },
            }
        ],
    }
    if usage:
        value["usage"] = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    return value


async def test_real_sdk_serializes_round_and_maps_multi_calls_without_reasoning() -> None:
    captured: list[dict[str, object]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.deepseek.com/chat/completions"
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncOpenAI(
            api_key="test-not-a-real-key",
            base_url="https://api.deepseek.com",
            http_client=http,
            max_retries=0,
        )
        provider = OpenAICompatibleChatProvider(
            ModelConfig(provider="deepseek"), "test-not-a-real-key", client=client
        )
        messages = (
            Message("system", "system"),
            Message("user", "task"),
            Message("assistant", "", (ToolCall("earlier", "git_status", "{}"),)),
            Message("tool", "clean", tool_call_id="earlier"),
        )
        reply = await provider.complete(
            ModelRequest(
                messages,
                (
                    {
                        "type": "function",
                        "function": {
                            "name": "git_status",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                ),
            )
        )
        assert [call.call_id for call in reply.tool_calls] == ["call-a", "call-b"]
        assert reply.text == "Visible text"
        assert reply.usage == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
        assert captured[0]["thinking"] == {"type": "disabled"}
        assert captured[0]["stream"] is False
        assert "parallel_tool_calls" not in captured[0]
        assert "hidden reasoning" not in repr(reply)
        assert "reasoning_content" not in json.dumps(captured)
        assert captured[0]["messages"][-1]["tool_call_id"] == "earlier"  # type: ignore[index]


async def test_sdk_error_does_not_leak_error_body_and_does_not_retry() -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429, json={"error": {"message": "sensitive-secret", "type": "rate_limit"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncOpenAI(api_key="private-key", http_client=http, max_retries=0)
        provider = OpenAICompatibleChatProvider(ModelConfig(), "private-key", client=client)
        with pytest.raises(ProviderError) as caught:
            await provider.complete(ModelRequest((Message("user", "test"),)))
        assert "429" in str(caught.value)
        assert "private-key" not in str(caught.value)
        assert "sensitive-secret" not in str(caught.value)
        assert calls == 1


async def test_length_and_missing_usage_remain_explicit() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion(reason="length", usage=False))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(),
            "fake",
            client=AsyncOpenAI(api_key="fake", http_client=http, max_retries=0),
        )
        reply = await provider.complete(ModelRequest(()))
        assert reply.stop_reason == "length"
        assert reply.usage is None
        assert provider.capabilities().streaming is False


async def test_capability_failure_has_no_network_request() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Network should not be attempted when tools are unsupported")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(supports_tools=False),
            "fake",
            client=AsyncOpenAI(api_key="fake", http_client=http),
        )
        with pytest.raises(ProviderError, match="does not support"):
            await provider.complete(ModelRequest((), ({"type": "function"},)))


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (408, True),
        (409, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
    ],
)
async def test_http_failures_classified_without_sdk_internal_retries(
    status: int, retryable: bool
) -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Retry-After": "12.5"},
            json={"error": {"message": "sensitive-secret", "type": "fixture"}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        # Even an injected client with its SDK default retries must be reduced to
        # one HTTP attempt, leaving all accounting/backoff to the runtime.
        provider = OpenAICompatibleChatProvider(
            ModelConfig(),
            "private-key",
            client=AsyncOpenAI(api_key="private-key", http_client=http),
        )
        with pytest.raises(ProviderError) as caught:
            await provider.complete(ModelRequest(()))
    assert calls == 1
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status
    assert caught.value.retry_after_seconds == 12.5
    assert caught.value.usage is None
    assert not caught.value.context_length_exceeded
    assert "sensitive-secret" not in str(caught.value)
    assert "private-key" not in str(caught.value)


@pytest.mark.parametrize("is_timeout", [False, True])
async def test_network_failures_are_sanitized_retryable_single_attempt(is_timeout: bool) -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        error = httpx.ReadTimeout if is_timeout else httpx.ConnectError
        raise error("sensitive-network-message", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(), "fake", client=AsyncOpenAI(api_key="fake", http_client=http)
        )
        with pytest.raises(ProviderError) as caught:
            await provider.complete(ModelRequest(()))
    assert calls == 1
    assert caught.value.retryable
    assert "sensitive-network-message" not in str(caught.value)
    assert caught.value.status_code is None


def test_retry_after_preserves_full_delay_and_supports_http_dates() -> None:
    now = datetime(2026, 10, 25, 0, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Sun, 25 Oct 2026 00:01:30 GMT", now=now) == 90.0
    assert parse_retry_after("Sun, 25 Oct 2026 00:00:00 GMT", now=now) == 0.0
    assert parse_retry_after("60") == 60.0
    assert parse_retry_after(" 0.5 ") == 0.5
    assert parse_retry_after("1000000000000") == 1000000000000.0
    assert parse_retry_after("9" * 1024) == math.inf
    assert parse_retry_after("-3") is None
    assert parse_retry_after("nan") is None
    assert parse_retry_after("inf") is None
    assert parse_retry_after("invalid-date") is None
    assert parse_retry_after(None) is None


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (400, {"code": "context_length_exceeded", "message": "private-vendor-body"}, True),
        (413, {"code": "context_window_exceeded", "message": "private-vendor-body"}, True),
        (400, {"type": "max_context_length_exceeded"}, True),
        (400, {"code": "CONTEXT_LENGTH_EXCEEDED"}, True),
        (
            400,
            {
                "message": "This model's maximum context length is 32768 tokens. "
                "However, you requested 40000 tokens. private-vendor-body"
            },
            True,
        ),
        (413, {"message": "Input token length exceeds the maximum context length"}, True),
        (400, {"message": "The context window has been exceeded"}, True),
        (400, {"message": "Input is too long for the model's context window"}, True),
        (400, {"message": "maximum output tokens exceeded private-vendor-body"}, False),
        (413, {"code": "payload_too_large", "message": "Request body too large"}, False),
        (400, {"type": "invalid_request_error", "message": "invalid tool schema"}, False),
        (400, {"message": "context length parameter is invalid"}, False),
        (400, {"message": "Invalid tool schema property context_length_exceeded"}, False),
        (
            400,
            {
                "message": "context length parameter must be positive; max_tokens exceeds output limit"
            },
            False,
        ),
        (429, {"code": "context_length_exceeded"}, False),
        (500, {"code": "context_length_exceeded"}, False),
    ],
)
async def test_context_length_error_has_distinct_sanitized_marker_for_both_provider_paths(
    streaming: bool, status: int, error: dict[str, str], expected: bool
) -> None:
    calls = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert json.loads(request.content)["stream"] is streaming
        return httpx.Response(status, json={"error": error, "private_key": "private-key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(supports_streaming=streaming),
            "private-key",
            client=AsyncOpenAI(api_key="private-key", http_client=http),
        )
        with pytest.raises(ProviderError) as caught:
            if streaming:
                async for _ in provider.stream(ModelRequest((Message("user", "task"),))):
                    pytest.fail("HTTP failure must not produce a response fragment")
            else:
                await provider.complete(ModelRequest((Message("user", "task"),)))
    failure = caught.value
    assert calls == 1
    assert failure.context_length_exceeded is expected
    assert failure.status_code == status
    assert failure.retryable is (status == 429 or status == 500)
    assert failure.usage is None
    assert not failure.partial_stream
    assert "private-vendor-body" not in str(failure)
    assert "private-key" not in str(failure)
    assert "private_key" not in repr(failure)
