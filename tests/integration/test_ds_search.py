"""Check the registered tool and the real SDK's two request formats."""

import json
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

from stellarweaver.config import AppConfig, LimitsConfig, ModelConfig, PolicyConfig
from stellarweaver.domain import JSONDict
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore

SOURCE = "https://nodejs.org/en/blog/release/"


def completion(
    message: JSONDict,
    stop: str = "stop",
    tool_search: int | None = None,
) -> httpx.Response:
    usage: JSONDict = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    if tool_search is not None:
        usage["server_tool_use"] = {"tool_search": tool_search}
    return httpx.Response(
        200,
        json={
            "id": "chat",
            "object": "chat.completion",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [
                {"index": 0, "finish_reason": stop, "message": {"role": "assistant", **message}}
            ],
            "usage": usage,
        },
    )


@pytest.mark.parametrize("case", ["found", "no_search", "budget"])
async def test_ds_search_tool_round_trip(tmp_path: Path, case: str) -> None:
    requests: list[JSONDict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        requests.append(wire)
        if len(requests) == 1:
            assert [tool["function"]["name"] for tool in wire["tools"]] == ["ds_search"]
            assert wire["thinking"] == {"type": "enabled"}
            return completion(
                {
                    "content": "",
                    "reasoning_content": "private-outer-reasoning",
                    "tool_calls": [
                        {
                            "id": "search",
                            "type": "function",
                            "function": {
                                "name": "ds_search",
                                "arguments": '{"query":"  Latest Node.js official release  "}',
                            },
                        }
                    ],
                },
                "tool_calls",
            )
        if len(requests) == 2:
            assert case != "budget"
            assert wire["tools"] == [{"type": "deepseek_web_search"}]
            assert wire["thinking"] == {"type": "disabled"}
            assert "reasoning_effort" not in wire
            assert len(wire["messages"]) == 1
            assert wire["messages"][0]["role"] == "user"
            assert wire["messages"][0]["content"].endswith("Latest Node.js official release")
            assert "outside search scope" not in wire["messages"][0]["content"]
            if case == "no_search":
                return completion({"content": "Unverified answer"})
            return completion(
                {
                    "content": "Retrieved a Node.js release.",
                    "server_tool_calls": [
                        {
                            "reasoning_content": "private-search-reasoning",
                            "calls": [
                                {
                                    "kind": "search",
                                    "status": "completed",
                                    "sources": [
                                        {"url": "javascript:alert(1)", "title": "Invalid source"},
                                        {"url": SOURCE, "title": "Node.js release"},
                                        {"url": SOURCE, "title": "Duplicate"},
                                    ],
                                }
                            ],
                        }
                    ],
                },
                tool_search=2,
            )
        assert wire["messages"][-2]["reasoning_content"] == "private-outer-reasoning"
        tool_result = json.loads(wire["messages"][-1]["content"])
        assert tool_result["status"] == ("ok" if case == "found" else "error")
        assert "private-search-reasoning" not in wire["messages"][-1]["content"]
        return completion({"content": "Final answer", "reasoning_content": ""})

    root = tmp_path / "chat"
    root.mkdir()
    model_config = ModelConfig(provider="deepseek")
    config = AppConfig(
        model=model_config,
        limits=LimitsConfig(max_model_calls=1 if case == "budget" else 3),
        policy=PolicyConfig(preset="read-only"),
    )
    async with AsyncOpenAI(
        api_key="fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    ) as client:
        provider = OpenAICompatibleChatProvider(model_config, "fixture", client=client)
        with SessionStore(root, tmp_path / "state") as store:
            agent = build_agent(
                config, provider, store, chat_only=True, enable_web_search=True, stream=False
            )
            outcome = await agent.run("Search the web. Keep this outside search scope.")
            assert outcome.model_calls == (1 if case == "budget" else 3)
            assert outcome.run_status == ("budget_exceeded" if case == "budget" else "completed")
            (result,) = store.load_results()
            if case == "found":
                assert result.data["sources"] == [{"url": SOURCE, "title": "Node.js release"}]
                assert (outcome.input_tokens, outcome.output_tokens) == (60, 30)
                events = store.load_events()
                (lookup,) = [
                    event
                    for event in events
                    if event.type == "model.started" and event.payload.get("purpose") == "ds_search"
                ]
                assert lookup.payload["thinking"] == "disabled"
                assert any(
                    (event.payload.get("usage") or {}).get("tool_search") == 2
                    for event in events
                    if event.type == "model.completed"
                )
            else:
                prefix = (
                    "WEB_SEARCH_BUDGET_EXCEEDED" if case == "budget" else "WEB_SEARCH_NO_SOURCES"
                )
                assert result.data["code"] == prefix
