"""Exercise aggregation through the installed SDK's actual SSE decoder."""

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from stellarweaver.config import ModelConfig
from stellarweaver.domain import AssistantReply, Message, ModelRequest
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider


def chunk(
    delta: dict[str, Any] | None = None,
    *,
    finish: str | None = None,
    usage: dict[str, int] | None = None,
    choices: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": "response-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if choices else [],
    }
    if usage is not None:
        result["usage"] = usage
    return result


def tool(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"index": index, "function": {}}
    if call_id is not None:
        value["id"] = call_id
        value["type"] = "function"
    if name is not None:
        value["function"]["name"] = name
    if arguments is not None:
        value["function"]["arguments"] = arguments
    return value


def sse(chunks: Sequence[dict[str, Any]], *, done: bool = True) -> bytes:
    content = "".join(f"data: {json.dumps(item, ensure_ascii=False)}\n\n" for item in chunks)
    return (content + ("data: [DONE]\n\n" if done else "")).encode("utf-8")


class SplitStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, disconnect: bool = False) -> None:
        self.data = data
        self.disconnect = disconnect
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        # One-byte transport frames split UTF-8, JSON escapes and SSE delimiters.
        for value in self.data:
            yield bytes([value])
        if self.disconnect:
            raise httpx.ReadError("secret-vendor-transport-body")

    async def aclose(self) -> None:
        self.closed = True


async def consume(
    chunks: Sequence[dict[str, Any]], *, disconnect: bool = False, max_tokens: int = 4096
) -> tuple[list[Any], dict[str, Any], SplitStream]:
    stream = SplitStream(sse(chunks, done=not disconnect), disconnect=disconnect)
    captured: dict[str, Any] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(provider="deepseek", supports_streaming=True),
            "not-a-real-key",
            client=AsyncOpenAI(api_key="not-a-real-key", http_client=http),
        )
        events = [
            event
            async for event in provider.stream(
                ModelRequest((Message("user", "task"),), max_output_tokens=max_tokens)
            )
        ]
    return events, captured, stream


async def test_sdk_sse_interleaves_fragmented_calls_text_unicode_and_usage() -> None:
    events, captured, stream = await consume(
        [
            chunk({"role": "assistant", "content": "先检查", "reasoning_content": "secret-chain"}),
            chunk({"tool_calls": [tool(1, call_id="call-b", name="git_", arguments="{")]}),
            chunk(
                {"tool_calls": [tool(0, call_id="call-a", name="read_", arguments='{"path":"中')]}
            ),
            chunk({"content": "文件", "tool_calls": [tool(1, name="status", arguments="}")]}),
            chunk(
                {"tool_calls": [tool(0, call_id="call-a", name="file", arguments='文\\n\\".py"}')]}
            ),
            chunk(finish="tool_calls"),
            chunk(choices=False),
            chunk(
                choices=False,
                usage={"prompt_tokens": 17, "completion_tokens": 13, "total_tokens": 30},
            ),
        ]
    )
    assert [event.type for event in events] == ["text.delta", "text.delta", "response.completed"]
    reply = events[-1].reply
    assert reply.text == "先检查文件"
    assert [call.call_id for call in reply.tool_calls] == ["call-a", "call-b"]
    assert [call.name for call in reply.tool_calls] == ["read_file", "git_status"]
    assert json.loads(reply.tool_calls[0].arguments_json) == {"path": '中文\n".py'}
    assert reply.tool_calls[1].arguments_json == "{}"
    assert reply.usage == {"prompt_tokens": 17, "completion_tokens": 13, "total_tokens": 30}
    assert "secret-chain" not in repr(events)
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert captured["thinking"] == {"type": "disabled"}
    assert "parallel_tool_calls" not in captured
    assert stream.closed


async def test_sdk_sse_missing_usage_and_invalid_json_are_not_repaired() -> None:
    events, _, _ = await consume(
        [
            chunk({"tool_calls": [tool(0, call_id="a", name="edit_file", arguments='{"old":')]}),
            chunk(finish="tool_calls"),
        ]
    )
    assert events[-1].reply.usage is None
    assert events[-1].reply.tool_calls[0].arguments_json == '{"old":'


async def test_sdk_sse_length_retains_truncated_arguments_as_incomplete_reply() -> None:
    events, _, _ = await consume(
        [
            chunk({"tool_calls": [tool(0, name="edit_", arguments='{"old":')]}),
            chunk(finish="length"),
        ]
    )
    assert events[-1].reply.stop_reason == "length"
    assert events[-1].reply.tool_calls[0].arguments_json == '{"old":'
    assert events[-1].reply.tool_calls[0].call_id == ""


@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        ([chunk({"content": "partial"})], "before a finish"),
        (
            [
                chunk({"tool_calls": [tool(0, call_id="a", name="read_file")]}),
                chunk({"tool_calls": [tool(0, call_id="b")]}),
                chunk(finish="tool_calls"),
            ],
            "changed a tool call ID",
        ),
        (
            [
                chunk(
                    {
                        "tool_calls": [
                            tool(0, call_id="a", name="read_file"),
                            tool(1, call_id="a", name="git_status"),
                        ]
                    }
                ),
                chunk(finish="tool_calls"),
            ],
            "duplicate",
        ),
        (
            [chunk({"tool_calls": [tool(0, name="read_file")]}), chunk(finish="tool_calls")],
            "incomplete",
        ),
        (
            [
                chunk(finish="stop"),
                chunk({"tool_calls": [tool(0, call_id="a", name="run_command")]}),
            ],
            "after its finish",
        ),
        ([chunk(finish="stop"), chunk(finish="stop")], "after its finish"),
        (
            [
                chunk({"tool_calls": [tool(-1, call_id="a", name="read_file")]}),
                chunk(finish="tool_calls"),
            ],
            "index",
        ),
        (
            [chunk({"tool_calls": [tool(0, call_id="a", name="read_file")]}), chunk(finish="stop")],
            "matching tool_calls",
        ),
        ([chunk(finish="tool_calls")], "without any calls"),
        ([chunk({"role": "tool"}), chunk(finish="stop")], "non-assistant"),
        (
            [
                chunk({"function_call": {"name": "read_file", "arguments": "{}"}}),
                chunk(finish="function_call"),
            ],
            "legacy",
        ),
    ],
)
async def test_sdk_sse_protocol_failures_never_emit_completed(
    chunks: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ProviderError, match=message):
        await consume(chunks)


async def test_sdk_sse_disconnect_preserves_known_usage_but_no_reply() -> None:
    usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    with pytest.raises(ProviderError) as caught:
        await consume(
            [chunk({"content": "partial"}), chunk(choices=False, usage=usage)], disconnect=True
        )
    assert caught.value.retryable
    assert caught.value.partial_stream
    assert caught.value.usage == usage
    assert "secret" not in str(caught.value)


async def test_sdk_sse_buffer_bound_prevents_unbounded_aggregation() -> None:
    with pytest.raises(ProviderError, match="bounded response buffer"):
        await consume([chunk({"content": "x" * 8193}), chunk(finish="stop")], max_tokens=1)


async def test_sdk_sse_invalid_json_is_a_sanitized_protocol_failure() -> None:
    stream = SplitStream(
        sse([chunk({"content": "partial"})], done=False) + b"data: private-invalid-json\n\n"
    )

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(supports_streaming=True),
            "fake",
            client=AsyncOpenAI(api_key="fake", http_client=http),
        )
        with pytest.raises(ProviderError, match="invalid protocol data") as caught:
            async for event in provider.stream(ModelRequest(())):
                events.append(event)
    assert [event.type for event in events] == ["text.delta"]
    assert caught.value.partial_stream
    assert "private-invalid-json" not in str(caught.value)
    assert stream.closed


async def test_streaming_tools_capability_failure_has_no_http_request() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Tool capability rejection must not send a request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(supports_tools=False, supports_streaming=True),
            "fake",
            client=AsyncOpenAI(api_key="fake", http_client=http),
        )
        with pytest.raises(ProviderError, match="does not support"):
            async for _ in provider.stream(ModelRequest((), ({"type": "function"},))):
                pass


async def test_stream_choice_index_must_be_the_single_requested_choice() -> None:
    value = chunk({"content": "unexpected choice"}, finish="stop")
    value["choices"][0]["index"] = 1
    with pytest.raises(ProviderError, match="exactly one"):
        await consume([value])


async def test_fake_stream_records_one_request_and_has_one_final_response() -> None:
    fake = FakeModel([AssistantReply("offline")])
    request = ModelRequest(())
    events = [event async for event in fake.stream(request)]
    assert fake.capabilities().streaming
    assert fake.requests == [request]
    assert [event.type for event in events] == ["text.delta", "response.completed"]
    assert events[-1].reply == AssistantReply("offline")


async def test_streaming_disabled_explicitly_falls_back_to_complete() -> None:
    captured: list[dict[str, Any]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "response-1",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "fallback"},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(supports_streaming=False),
            "fake",
            client=AsyncOpenAI(api_key="fake", http_client=http),
        )
        events = [event async for event in provider.stream(ModelRequest(()))]
    assert not provider.capabilities().streaming
    assert [event.type for event in events] == ["response.completed"]
    assert events[0].reply.text == "fallback"
    assert len(captured) == 1
    assert captured[0]["stream"] is False
    assert "stream_options" not in captured[0]
