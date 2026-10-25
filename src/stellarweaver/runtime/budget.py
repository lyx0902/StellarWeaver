"""Task resource lifetimes with bounded, cancellation-resistant shutdown."""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

Preflight = Callable[[], AbstractAsyncContextManager[None]]


@asynccontextmanager
async def empty_preflight() -> AsyncIterator[None]:
    yield


async def finish_cleanup(operation: Awaitable[Any], *, grace_seconds: float = 15.0) -> bool:
    """Wait for owned cleanup before reporting; repeated cancellation does not skip it.

    The grace deadline is separate from the task deadline. Components must retain
    their own bounded subprocess shutdown contracts when cancellation is requested.
    Return whether the caller was interrupted while cleanup was running.
    """

    async def bounded() -> None:
        started = time.monotonic()
        await asyncio.wait_for(operation, timeout=grace_seconds)
        # wait_for may return normally after an operation catches cancellation
        # and finishes its remaining owned resources. Keep waiting for that
        # cleanup, but never turn a missed grace deadline into success.
        if time.monotonic() - started >= grace_seconds:
            raise TimeoutError("Resource cleanup exceeded its grace deadline")

    task = asyncio.create_task(bounded())
    interrupted = False
    while True:
        try:
            await asyncio.shield(task)
            return interrupted
        except asyncio.CancelledError:
            if task.done():
                task.result()
            interrupted = True
