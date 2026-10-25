"""SDK-, UI- and storage-independent domain contracts."""

from dataclasses import dataclass, field
from typing import Any, Literal

JSONDict = dict[str, Any]
ToolStatus = Literal["ok", "error", "denied", "cancelled", "blocked", "unknown"]
RunStatus = Literal["completed", "blocked", "cancelled", "budget_exceeded", "failed"]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments_json: str


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    operation_id: str
    tool_name: str
    status: ToolStatus
    content: str
    data: JSONDict = field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    duration_ms: int = 0


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class AssistantReply:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "stop"
    usage: JSONDict | None = None


@dataclass(frozen=True)
class ModelRequest:
    messages: tuple[Message, ...]
    tools: tuple[JSONDict, ...] = ()
    max_output_tokens: int = 4096


@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool = True
    streaming: bool = False
    parallel_tool_calls: bool = False
    context_window_tokens: int | None = 100_000


@dataclass(frozen=True)
class AgentEvent:
    session_id: str
    run_id: str
    seq: int
    type: str
    payload: JSONDict
    timestamp: str
    schema_version: int = 1


@dataclass(frozen=True)
class RunOutcome:
    run_status: RunStatus
    task_assessment: Literal["solved", "partial", "unknown"]
    verification_status: Literal["passed", "failed", "not_run"]
    evidence: tuple[JSONDict, ...] = ()
    final_text: str = ""
    reason: str = ""
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_estimated: bool = False
    duration_ms: int = 0
    session_id: str = ""
    run_id: str = ""
    state_path: str = ""
    usage_incomplete: bool = False


@dataclass(frozen=True)
class ModelStreamEvent:
    type: Literal["text.delta", "response.completed"]
    text: str = ""
    reply: AssistantReply | None = None


@dataclass(frozen=True)
class TextDelta:
    session_id: str
    run_id: str
    request_index: int
    chunk_index: int
    text: str
