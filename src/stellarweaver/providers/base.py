"""Provider boundary: one complete response, no execution authority."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from stellarweaver.domain import (
    AssistantReply,
    JSONDict,
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
)


class ProviderError(RuntimeError):
    """A sanitized provider failure safe to put in the run record."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
        status_code: int | None = None,
        usage: JSONDict | None = None,
        partial_stream: bool = False,
        context_length_exceeded: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code
        self.usage = usage
        self.partial_stream = partial_stream
        # This is a distinct bounded compaction path, not a transient HTTP retry.
        self.context_length_exceeded = context_length_exceeded


@runtime_checkable
class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> AssistantReply: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...

    def capabilities(self) -> ModelCapabilities: ...
