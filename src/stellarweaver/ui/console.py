"""Plain event output and explicit scoped approvals."""

import asyncio
import json
import sys
from dataclasses import asdict
from typing import Any

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.syntax import Syntax

from stellarweaver.domain import AgentEvent, RunOutcome, TextDelta


class TerminalUI:
    def __init__(self, *, json_output: bool = False) -> None:
        self.json_output = json_output
        self.console = Console(stderr=False)
        self.prompts: PromptSession[str] | None = None
        self._stream_request: tuple[str, int] | None = None
        self._stream_text = ""
        self._visible_final_text = ""
        self._line_open = False

    def notice(self, text: str) -> None:
        self._close_line()
        self.console.print(text, markup=False)

    def _close_line(self) -> None:
        if self._line_open:
            self.console.print()
            self._line_open = False

    def delta(self, delta: TextDelta) -> None:
        if self.json_output:
            print(
                json.dumps(
                    {"schema_version": 1, "type": "text.delta", **asdict(delta)},
                    ensure_ascii=True,
                ),
                flush=True,
            )
            return
        if not delta.text:
            return
        request = (delta.run_id, delta.request_index)
        if request != self._stream_request:
            self._close_line()
            self._stream_request = request
            self._stream_text = ""
        self._stream_text += delta.text
        self.console.print(delta.text, end="", markup=False, highlight=False, soft_wrap=True)
        self._line_open = bool(delta.text) and not delta.text.endswith("\n")

    def event(self, event: AgentEvent) -> None:
        if self.json_output:
            # ASCII escapes keep JSONL valid even on legacy Windows GBK stdout.
            # JSON parsers recover the original Unicode text without loss.
            print(json.dumps(asdict(event), ensure_ascii=True), flush=True)
        elif event.type == "run.started":
            self._close_line()
            self._stream_request = None
            self._stream_text = self._visible_final_text = ""
            self.console.print(f"Workspace: {event.payload['workspace']}", markup=False)
        elif event.type == "assistant.completed":
            self._close_line()
            content = event.payload.get("content", "")
            self._visible_final_text = content if self._stream_text == content else ""
            self._stream_text = ""
        elif event.type == "model.interrupted":
            self._close_line()
            self._stream_text = self._visible_final_text = ""
            self.console.print(
                "Stream interrupted; displayed draft discarded. No partial tool call was executed.",
                markup=False,
                style="yellow",
            )
        elif event.type == "model.retry_scheduled":
            self.notice("Retrying the model request within the remaining budget.")
        elif event.type == "model.started" and event.payload.get("stream_fallback"):
            self.notice("Provider streaming unavailable; using a complete response.")
        elif event.type == "tool.started":
            self._close_line()
            name = event.payload.get("tool", event.payload.get("tool_name", ""))
            detail = event.payload.get("path")
            if event.payload.get("argv") is not None:
                detail = json.dumps(event.payload["argv"], ensure_ascii=False)
            self.console.print(f"tool {name}: {detail or ''}", markup=False, style="cyan")
        elif event.type == "tool.completed":
            self.console.print(
                f"result {event.payload.get('tool_name', '')}: {event.payload.get('status', '')} "
                f"({event.payload.get('duration_ms', 0)} ms)",
                markup=False,
            )
            artifacts = event.payload.get("artifact_ids")
            if artifacts:
                self.console.print(f"Output artifacts: {', '.join(artifacts)}", markup=False)

    async def approve(self, invocation: dict[str, Any]) -> str:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return "deny"
        self.console.print("Approval required", style="bold yellow")
        self.console.print(f"Workspace: {invocation['workspace']}", markup=False)
        self.console.print(f"Tool: {invocation['tool']}", markup=False)
        if invocation.get("argv") is not None:
            self.console.print(
                "argv: " + json.dumps(invocation["argv"], ensure_ascii=False), markup=False
            )
            self.console.print(f"cwd: {invocation.get('cwd', '.')}", markup=False)
            self.console.print("Host commands run with your OS user permissions.", markup=False)
        if invocation.get("path") is not None:
            self.console.print(f"Path: {invocation['path']}", markup=False)
        preview = invocation.get("preview") or invocation.get("diff")
        if preview:
            self.console.print(Syntax(str(preview), "diff", word_wrap=True))
        if self.prompts is None:
            self.prompts = PromptSession()
        try:
            answer = (
                (
                    await self.prompts.prompt_async(
                        "[o]nce / [s]ame scope this session / [d]eny: ", handle_sigint=False
                    )
                )
                .strip()
                .lower()
            )
        except EOFError:
            return "deny"
        except KeyboardInterrupt:
            raise asyncio.CancelledError from None
        return {"o": "once", "once": "once", "s": "session", "session": "session"}.get(
            answer, "deny"
        )

    def outcome(self, outcome: RunOutcome) -> None:
        if self.json_output:
            return
        self._close_line()
        if outcome.final_text and outcome.final_text != self._visible_final_text:
            self.console.print(outcome.final_text, markup=False)
        self.console.print(
            f"Run: {outcome.run_status}; task: {outcome.task_assessment}; "
            f"verification: {outcome.verification_status}",
            markup=False,
        )
        if outcome.reason:
            self.console.print(outcome.reason, markup=False)
        self.console.print(f"Session evidence: {outcome.state_path}", markup=False)
