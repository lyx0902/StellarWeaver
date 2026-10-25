"""Small asynchronous chat controller with testable, cleanup-aware cancellation."""

from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import Awaitable, Callable
from types import FrameType
from typing import Any

from stellarweaver.domain import RunOutcome
from stellarweaver.ui.console import TerminalUI

PromptReader = Callable[[str], Awaitable[str]]
RunCaller = Callable[[str, bool], Awaitable[RunOutcome]]
StatusReader = Callable[[], str]
CommandCaller = Callable[[str, str], Awaitable[str]]


class ChatLoop:
    """Keep input and one managed agent task in one event loop.

    The injected run caller reuses the same agent, policy and session. A second
    interrupt requests exit *after* the first cancellation's cleanup finishes.
    """

    def __init__(
        self,
        reader: PromptReader,
        run: RunCaller,
        ui: TerminalUI,
        status: StatusReader,
        *,
        install_signal_handler: bool = True,
        command: CommandCaller | None = None,
    ) -> None:
        self.reader = reader
        self.run_task = run
        self.ui = ui
        self.status = status
        self.install_signal_handler = install_signal_handler
        self.command = command
        self.active: asyncio.Task[RunOutcome | str] | None = None
        self.exit_requested = False
        self._interrupts = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._signal_wakeup: asyncio.TimerHandle | None = None

    def interrupt(self) -> None:
        """Run on the event-loop thread; never interrupt cleanup a second time."""
        if self.active is None or self.active.done():
            return
        self._interrupts += 1
        if self._interrupts == 1:
            self.active.cancel()
        else:
            self.exit_requested = True

    def _signal(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.interrupt)

    def _signal_tick(self) -> None:
        """Wake a Windows Proactor wait so Python can dispatch pending SIGINT.

        A timer callback keeps the interpreter responsive while the only work
        is an otherwise idle network request or child-process pipe. It does not
        cancel the task and remains active during cancellation cleanup.
        """
        self._signal_wakeup = None
        if self._loop is not None and self.active is not None and not self.active.done():
            self._signal_wakeup = self._loop.call_later(0.1, self._signal_tick)

    async def _run(
        self, text: str, continued: bool, *, is_command: bool = False
    ) -> RunOutcome | str:
        if is_command:
            parts = text.split(maxsplit=1)
            name, argument = parts[0], parts[1] if len(parts) == 2 else ""
            if self.command is None:
                return "This command is unavailable in this chat."
            return await self.command(name, argument.strip())
        return await self.run_task(text, continued)

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        continued = False
        idle_interrupts = 0
        self.ui.notice(
            "Persistent chat. /help for commands; /exit to finish. "
            "Budgets reset for each submitted task; conversation and scoped approvals persist."
        )
        while True:
            try:
                text = (await self.reader("StellarWeaver> ")).strip()
            except EOFError:
                return 0
            except KeyboardInterrupt:
                idle_interrupts += 1
                if idle_interrupts >= 2:
                    return 130
                self.ui.notice("Input cleared. Press Ctrl+C again to exit.")
                continue
            if not text:
                continue
            idle_interrupts = 0
            if text in {"/exit", "/quit"}:
                return 0
            if text == "/help":
                self.ui.notice(
                    "/help: show commands; /status: show model, session and budgets; "
                    "/model [NAME]: inspect or switch model; /plan [build]: select read-only or restore initial policy; "
                    "/diff: inspect changes; /compact: compress current context; /undo: undo verified built-in edits; "
                    "/skill NAME: load an explicit skill; /clear: begin a new context boundary; "
                    "/exit: close chat. Ctrl+C cancels a task and returns to input."
                )
                continue
            if text == "/status":
                self.ui.notice(self.status())
                continue
            is_command = text.startswith("/")
            if is_command and text.split(maxsplit=1)[0] not in {
                "/model",
                "/plan",
                "/diff",
                "/compact",
                "/undo",
                "/skill",
                "/clear",
            }:
                self.ui.notice("Unknown command. Use /help.")
                continue
            self._interrupts = 0
            self.active = asyncio.create_task(self._run(text, continued, is_command=is_command))
            previous_handler: Any = None
            owns_handler = self.install_signal_handler and (
                threading.current_thread() is threading.main_thread()
            )
            if owns_handler:
                previous_handler = signal.signal(signal.SIGINT, self._signal)
                self._signal_tick()
            try:
                outcome = await self.active
                if isinstance(outcome, RunOutcome):
                    self.ui.outcome(outcome)
                else:
                    self.ui.notice(outcome)
            except asyncio.CancelledError:
                # A conforming Agent returns a cancelled outcome after cleanup.
                # Still restore the signal handler when an injected caller raises.
                self.ui.notice("Task cancelled.")
            finally:
                if self._signal_wakeup is not None:
                    self._signal_wakeup.cancel()
                    self._signal_wakeup = None
                if owns_handler:
                    signal.signal(signal.SIGINT, previous_handler)
                self.active = None
            if not is_command:
                continued = True
            if self.exit_requested:
                return 130
