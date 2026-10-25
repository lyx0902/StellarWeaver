"""Deterministic offline model, supporting fixed scripts and request-aware callbacks."""

import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from stellarweaver.domain import (
    AssistantReply,
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
    ToolCall,
)
from stellarweaver.providers.base import ProviderError

ReplyCallback = Callable[[ModelRequest], AssistantReply | Awaitable[AssistantReply]]


class ScriptCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    call_id: str
    name: str
    arguments_json: str


class ScriptReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = ""
    tool_calls: list[ScriptCall] = Field(default_factory=list)
    stop_reason: str = "stop"
    usage: dict[str, Any] | None = None


class FakeModel:
    def __init__(
        self,
        replies: Sequence[AssistantReply] | ReplyCallback | None = None,
        *,
        callback: ReplyCallback | None = None,
    ) -> None:
        self.requests: list[ModelRequest] = []
        self._callback = (
            callback if callback is not None else (replies if callable(replies) else None)
        )
        self._replies = list(replies) if replies is not None and not callable(replies) else []
        self._scripted = replies is not None or callback is not None

    async def complete(self, request: ModelRequest) -> AssistantReply:
        self.requests.append(request)
        if self._callback is not None:
            result = self._callback(request)
            return await result if inspect.isawaitable(result) else result
        if self._replies:
            return self._replies.pop(0)
        if self._scripted:
            raise ProviderError("FakeModel script exhausted before a final response")
        return AssistantReply(
            text="Offline FakeModel: no scripted actions were requested; no model connectivity was tested."
        )

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(streaming=True)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        reply = await self.complete(request)
        if reply.text:
            yield ModelStreamEvent("text.delta", text=reply.text)
        yield ModelStreamEvent("response.completed", reply=reply)

    @classmethod
    def from_script(cls, path: Path) -> "FakeModel":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("FakeModel script must be a JSON array of assistant replies")
        replies = [ScriptReply.model_validate(item) for item in value]
        return cls(
            [
                AssistantReply(
                    text=reply.text,
                    tool_calls=tuple(ToolCall(**call.model_dump()) for call in reply.tool_calls),
                    stop_reason=reply.stop_reason,
                    usage=reply.usage,
                )
                for reply in replies
            ]
        )
