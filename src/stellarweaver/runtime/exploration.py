"""A bounded read-only child run with private context and parent-owned model accounting."""

from pathlib import Path
from time import monotonic
from uuid import uuid4

from pydantic import Field

from stellarweaver.config import HarnessConfig, LimitsConfig
from stellarweaver.context.manager import ContextSize
from stellarweaver.domain import (
    AgentEvent,
    AssistantReply,
    JSONDict,
    Message,
    ModelRequest,
    ToolResult,
)
from stellarweaver.policy import PolicyEngine
from stellarweaver.runtime.loop import Agent, BudgetExceeded
from stellarweaver.session.store import now
from stellarweaver.tools import files, search
from stellarweaver.tools.files import Arguments
from stellarweaver.tools.navigation import register_navigation
from stellarweaver.tools.registry import PreparedTool, ToolContext, ToolRegistry
from stellarweaver.tools.workspace import MAX_EDIT_BYTES, ToolError, Workspace, sha256_bytes


class ExplorationArgs(Arguments):
    goal: str = Field(min_length=1, max_length=4096)
    paths: list[str] = Field(default_factory=list, max_length=12)


class ExplorationJournal:
    """Keep only the current private event; the child Agent owns its messages."""

    def __init__(self, workspace: Path, directory: Path) -> None:
        self.workspace = workspace
        self.directory = self.artifacts_dir = self.path = directory
        self.session_id = self.run_id = uuid4().hex
        self.sequence = 0
        self.last_event = self.append_event("exploration.created", {})
        self.operations: dict[str, str] = {}
        self.result_events: dict[str, AgentEvent] = {}

    def close(self) -> None:
        pass

    def append_message(self, message: Message) -> str:
        return uuid4().hex

    def append_event(self, kind: str, payload: JSONDict) -> AgentEvent:
        self.sequence += 1
        self.last_event = AgentEvent(
            self.session_id, self.run_id, self.sequence, kind, payload, now()
        )
        return self.last_event

    def record_assistant(self, message: Message) -> str:
        self.operations = {call.call_id: uuid4().hex for call in message.tool_calls}
        self.append_event("assistant.completed", {})
        return uuid4().hex

    def operation_id_for(self, assistant_id: str, call_id: str) -> str:
        return self.operations[call_id]

    def record_approved(self, assistant_id: str, payload: JSONDict) -> AgentEvent:
        return self.append_event("tool.authorization", payload)

    def record_intent(self, assistant_id: str, payload: JSONDict) -> AgentEvent:
        return self.append_event("tool.started", payload)

    def record_result(self, assistant_id: str, result: ToolResult) -> AgentEvent:
        # Agent publishes the registry receipt and then handles the same result.
        event = self.result_events.get(result.operation_id)
        if event is None:
            event = self.append_event(
                "tool.completed",
                {
                    "tool_name": result.tool_name,
                    "status": result.status,
                    "operation_id": result.operation_id,
                    "call_id": result.call_id,
                },
            )
            self.result_events[result.operation_id] = event
        return event

    def load_events(self) -> tuple[AgentEvent, ...]:
        return (self.last_event,)

    def write_checkpoint(self, summary: JSONDict, through_event_seq: int) -> str:
        identifier = uuid4().hex
        self.append_event("context.checkpoint", {"checkpoint_id": identifier})
        return identifier

    def finish_run(self, outcome: JSONDict, event_type: str) -> AgentEvent:
        return self.append_event(event_type, {})


class ExplorationAgent(Agent):
    def __init__(
        self,
        parent: Agent,
        registry: ToolRegistry,
        journal: ExplorationJournal,
        identity: str,
        limits: LimitsConfig,
        deadline: float,
    ) -> None:
        self.parent, self.identity, self.deadline = parent, identity, deadline
        super().__init__(
            parent.provider,
            registry,
            limits=limits,
            store=journal,
            on_event=self.trace,
            max_output_tokens=min(1500, parent.max_output_tokens),
            context_config=parent.context_config,
            harness_config=HarnessConfig(
                smart_tool_truncation=parent.harness_config.smart_tool_truncation
            ),
            system_prompt=(
                "You are a read-only repository exploration assistant with a small shared budget. "
                "Inspect relevant files and return a concise evidence-based answer. "
                "Use only your listed tools and never request write access or external work."
            ),
        )

    async def trace(self, event: AgentEvent) -> None:
        if event.type in {"tool.started", "tool.completed"}:
            payload = event.payload
            await self.parent._emit(
                "exploration.tool",
                {
                    "exploration_id": self.identity,
                    "event": event.type,
                    "tool_name": payload.get("tool_name", payload.get("tool")),
                    "operation_id": payload["operation_id"],
                    "status": payload.get("status"),
                },
            )

    def _remaining(self) -> float:
        left = self.deadline - monotonic()
        if left <= 0:
            raise BudgetExceeded("shared exploration deadline")
        return left

    async def _request_model(
        self,
        initial_request: ModelRequest,
        input_size: ContextSize,
        *,
        exploration_id: str | None = None,
    ) -> AssistantReply:
        request = self._checked_request(initial_request, input_size.tokens)
        parent = self.parent
        model, tokens_in, tokens_out = (
            parent._model_calls,
            parent._input_tokens,
            parent._output_tokens,
        )
        try:
            return await parent._request_model(
                request, input_size, exploration_id=self.identity
            )
        finally:
            self._model_calls += parent._model_calls - model
            self._input_tokens += parent._input_tokens - tokens_in
            self._output_tokens += parent._output_tokens - tokens_out


def _matched_hash(workspace: Workspace, match: JSONDict) -> str | None:
    try:
        path = workspace.resolve(match["path"])
        if workspace.is_ignored(match["path"]):
            return None
        with path.open("rb") as stream:
            data = stream.read(MAX_EDIT_BYTES + 1)
        line = match["line"] - 1
        lines = data.decode("utf-8").splitlines() if len(data) <= MAX_EDIT_BYTES else []
        if 0 <= line < len(lines) and lines[line].startswith(match["text"]):
            return sha256_bytes(data)
    except (KeyError, TypeError, OSError, UnicodeError, ToolError):
        pass
    return None


def _evidence(results: list[ToolResult], workspace: Workspace) -> list[JSONDict]:
    found: dict[str, str] = {}
    for result in results:
        if result.status != "ok":
            continue
        entries = (
            [result.data] if result.tool_name == "read_file" else result.data.get("matches", [])
        )
        for item in entries[:16]:
            path = item.get("path")
            if not isinstance(path, str):
                continue
            value = item.get("sha256")
            if result.tool_name == "search_text" and path not in found:
                value = _matched_hash(workspace, item)
            if isinstance(value, str) and len(value) == 64:
                found[path] = value
            if len(found) == 16:
                return [{"path": name, "sha256": digest} for name, digest in found.items()]
    return [{"path": name, "sha256": digest} for name, digest in found.items()]


def register_exploration(parent: Agent) -> None:
    async def prepare(args: ExplorationArgs, ctx: ToolContext) -> PreparedTool:
        if not args.goal.strip():
            raise ToolError("INVALID_ARGUMENTS", "Exploration requires a nonempty goal.")
        paths = [
            ctx.workspace.relative(ctx.workspace.resolve(path, allow_directory=True))
            for path in args.paths
        ]

        async def execute() -> JSONDict:
            model_left = parent.limits.max_model_calls
            counts = {
                "max_model_calls": min(
                    4, model_left - parent._model_calls if model_left is not None else 4
                ),
                "max_tool_calls": min(
                    12,
                    parent.limits.max_tool_calls - parent._tool_calls - parent._completion_attempts,
                ),
                "max_input_tokens_total": parent.limits.max_input_tokens_total
                - parent._input_tokens,
                "max_output_tokens_total": parent.limits.max_output_tokens_total
                - parent._output_tokens,
            }
            deadline = min(parent._start + parent.limits.max_wall_seconds, monotonic() + 90)
            if min(counts.values()) <= 0 or deadline <= monotonic():
                raise ToolError(
                    "EXPLORATION_BUDGET_EXCEEDED", "Parent has insufficient exploration budget."
                )
            root = ctx.workspace.root
            journal = ExplorationJournal(root, parent.store.directory)
            registry = ToolRegistry(
                root,
                PolicyEngine("read-only", workspace=root),
                builtins=False,
                artifacts_dir=parent.store.artifacts_dir,
            )
            for protected in ctx.workspace.protected_roots:
                registry.workspace.protect(protected)
            files.register_tools(registry, readonly_only=True)
            search.register_tools(registry)
            register_navigation(registry, prefilter=parent.harness_config.navigation_prefilter)
            child = ExplorationAgent(
                parent, registry, journal, ctx.operation_id, LimitsConfig(**counts), deadline
            )
            child._scope_paths.update(paths)
            await parent._emit(
                "exploration.started", {"exploration_id": ctx.operation_id, "paths": paths}
            )
            try:
                outcome = await child.run(args.goal)
            finally:
                parent._tool_calls += child._tool_calls
            await parent._emit(
                "exploration.completed",
                {
                    "exploration_id": ctx.operation_id,
                    "status": outcome.run_status,
                    "model_calls": child._model_calls,
                    "tool_calls": child._tool_calls,
                    "input_tokens": child._input_tokens,
                    "output_tokens": child._output_tokens,
                },
            )
            return {
                "status": "cancelled" if outcome.run_status == "cancelled" else "ok",
                "content": outcome.final_text[:6000]
                or f"Exploration ended with status {outcome.run_status}.",
                "data": {"evidence": _evidence(child.results, registry.workspace)},
            }

        return PreparedTool({"paths": paths}, execute)

    parent.registry.register(
        "delegate_exploration",
        "Delegate bounded read-only source exploration and return evidence.",
        ExplorationArgs,
        prepare,
        read_only=True,
    )
