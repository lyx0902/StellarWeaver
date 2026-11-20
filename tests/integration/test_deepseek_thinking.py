"""Exercise DeepSeek request replay through the SDK, agent and SQLite journal."""

import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from stellarweaver.config import AppConfig, ContextConfig, ModelConfig, PolicyConfig
from stellarweaver.context import ContextManager
from stellarweaver.domain import JSONDict, Message, ModelRequest, ToolCall
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore

PRIVATE = "private-provider-state"


def response(
    *, streaming: bool, calls: list[JSONDict], text: str, missing: bool = False
) -> httpx.Response:
    parts: JSONDict = {"role": "assistant", "content": text}
    if not missing and not streaming:
        parts["reasoning_content"] = PRIVATE
    if calls:
        parts["tool_calls"] = calls
    envelope: JSONDict = {
        "id": "answer",
        "created": 1,
        "model": "deepseek-flash",
        "system_fingerprint": "fp_fixture",
    }
    usage = {
        "prompt_tokens": 21,
        "completion_tokens": 10,
        "total_tokens": 31,
        "prompt_cache_hit_tokens": 5,
        "prompt_cache_miss_tokens": 16,
        "completion_tokens_details": {"reasoning_tokens": 8},
    }
    stop = "tool_calls" if calls else "stop"
    if not streaming:
        return httpx.Response(
            200,
            json={
                **envelope,
                "object": "chat.completion",
                "usage": usage,
                "choices": [{"index": 0, "message": parts, "finish_reason": stop}],
            },
        )

    deltas = (
        []
        if missing
        else [
            {"reasoning_content": PRIVATE[:8]},
            {"reasoning_content": PRIVATE[8:]},
        ]
    )
    deltas.append(parts)
    frames = [
        {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas
    ]
    frames.append({"choices": [{"index": 0, "delta": {}, "finish_reason": stop}]})
    frames.append({"choices": [], "usage": usage})
    content = "".join(
        f"data: {json.dumps({**envelope, 'object': 'chat.completion.chunk', **frame})}\n\n"
        for frame in frames
    )
    return httpx.Response(
        200, headers={"Content-Type": "text/event-stream"}, content=content + "data: [DONE]\n\n"
    )


@pytest.mark.parametrize("streaming", [True, False])
async def test_thinking_roundtrip_and_reopen(tmp_path: Path, streaming: bool) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "marker.txt").write_text("fixture-value", encoding="utf-8")
    config = AppConfig(
        model=ModelConfig(provider="deepseek", supports_streaming=streaming),
        policy=PolicyConfig(preset="read-only"),
    )
    requests: list[JSONDict] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = requests[-1]
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "high"
        if len(requests) == 1:
            calls = [
                {
                    **({"index": index} if streaming else {}),
                    "id": identifier,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
                for index, (identifier, name, arguments) in enumerate(
                    [
                        ("read", "read_file", '{"path":"marker.txt"}'),
                        ("list", "list_files", '{"path":"."}'),
                    ]
                )
            ]
            return response(streaming=streaming, calls=calls, text="")
        if len(requests) == 2:
            assistant, read, listing = body["messages"][-3:]
            assert assistant["reasoning_content"] == PRIVATE
            assert [call["id"] for call in assistant["tool_calls"]] == ["read", "list"]
            assert [read["tool_call_id"], listing["tool_call_id"]] == ["read", "list"]
        else:
            assert not any(item["role"] in {"assistant", "tool"} for item in body["messages"])
            summary = next(
                item["content"]
                for item in body["messages"]
                if item["content"].startswith("Recorded conversation evidence")
            )
            assert "fixture-value" in summary
            assert "read_file" in summary
        return response(streaming=streaming, calls=[], text="fixture-value")

    state = tmp_path / "state"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = AsyncOpenAI(api_key="fixture", http_client=http)
        try:
            with SessionStore(root, state) as store:
                session = store.session_id
                provider = OpenAICompatibleChatProvider(config.model, "fixture", client=client)
                agent = build_agent(config, provider, store, stream=streaming)
                first = await agent.run("Read the marker")
                assert first.run_status == "completed", first.reason
                assert first.final_text == "fixture-value"
                assert (first.model_calls, first.tool_calls) == (2, 2)
                assert (first.input_tokens, first.output_tokens, first.usage_estimated) == (
                    42,
                    20,
                    False,
                )
                assert agent.messages[-1].reasoning_content == PRIVATE
                assert all(msg.reasoning_content is None for msg in store.load_messages())
                for item in store.load_events():
                    assert PRIVATE not in json.dumps(item.payload)
                    if item.type == "model.completed":
                        assert item.payload["reasoning_tokens"] == 8
                        assert item.payload["prompt_cache_hit_tokens"] == 5
                        assert item.payload["response_model"] == "deepseek-flash"
                sqlite = store.path
            assert PRIVATE.encode() not in sqlite.read_bytes()
            with SessionStore.open(session, data_dir=state) as store:
                provider = OpenAICompatibleChatProvider(config.model, "fixture", client=client)
                agent = build_agent(config, provider, store, stream=streaming)
                follow_up = await agent.resume("Repeat the marker")
                assert follow_up.run_status == "completed", follow_up.reason
                assert follow_up.final_text == "fixture-value"
        finally:
            await client.close()
    assert len(requests) == 3


@pytest.mark.parametrize("streaming", [True, False])
async def test_missing_replay_state_commits_no_tools(tmp_path: Path, streaming: bool) -> None:
    args = {"id": "a", "type": "function", "function": {"name": "list_files", "arguments": "{}"}}
    if streaming:
        args["index"] = 0
    transport = httpx.MockTransport(
        lambda _: response(streaming=streaming, calls=[args], text="", missing=True)
    )
    config = AppConfig(model=ModelConfig(provider="deepseek", supports_streaming=streaming))
    async with httpx.AsyncClient(transport=transport) as http:
        client = AsyncOpenAI(api_key="fixture", http_client=http)
        try:
            provider = OpenAICompatibleChatProvider(config.model, "fixture", client=client)
            with SessionStore(tmp_path, tmp_path / "state") as store:
                outcome = await build_agent(config, provider, store, stream=streaming).run("task")
                assert outcome.run_status == "failed"
                assert outcome.tool_calls == 0
                assert store.load_results() == ()
        finally:
            await client.close()


@pytest.mark.parametrize("streaming", [True, False])
async def test_toolless_thinking_discards_raw_reasoning(streaming: bool) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "tools" not in body
        assert body["messages"][0] == {"role": "assistant", "content": "Earlier"}
        return response(streaming=streaming, calls=[], text="Reply")

    async with AsyncOpenAI(
        api_key="fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as client:
        provider = OpenAICompatibleChatProvider(
            ModelConfig(provider="deepseek", supports_streaming=streaming), "fixture", client=client
        )
        request = ModelRequest(
            (Message("assistant", "Earlier", reasoning_content=PRIVATE), Message("user", "Next"))
        )
        if streaming:
            events = [item async for item in provider.stream(request)]
            assert [item.text for item in events if item.type == "text.delta"] == ["Reply"]
            reply = events[-1].reply
        else:
            reply = await provider.complete(request)
        assert reply is not None and reply.text == "Reply"
        assert reply.reasoning_content is None
        assert reply.usage is not None and reply.usage["reasoning_tokens"] == 8


def test_compaction_preserves_private_state_of_retained_round() -> None:
    messages = [Message("system", "rules"), Message("user", "task")]
    for index in range(4):
        identifier = str(index)
        messages.extend(
            [
                Message(
                    "assistant",
                    tool_calls=(ToolCall(identifier, "read_file", "{}"),),
                    reasoning_content=f"{PRIVATE}-{index}",
                ),
                Message("tool", "evidence " * 2000, tool_call_id=identifier),
            ]
        )
    context = ContextManager().build(
        messages,
        (),
        100,
        3_500,
        ContextConfig(recent_complete_rounds=1, reserve_tokens=0),
        force=True,
    )
    projected = context.messages
    retained = [item for item in projected if item.role == "assistant"]
    assert context.compacted
    assert 0 < len(retained) < 4
    assert retained[-1].reasoning_content == f"{PRIVATE}-3"
    assert [item.tool_call_id for item in projected if item.role == "tool"] == [
        item.tool_calls[0].call_id for item in retained
    ]
    assert all(PRIVATE not in message.content for message in projected)
