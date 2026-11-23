"""Private G5 verification service. The worker supplies only a snapshot and a command index."""

import asyncio
import os
import re
import secrets
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, BinaryIO
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request

from stellarweaver.config import ExecutionConfig
from stellarweaver.domain import JSONDict
from stellarweaver.tools.commands import _wait_cleanup
from stellarweaver.tools.development import Action, DevelopmentArgs, development_argv
from stellarweaver.tools.workspace import ToolError, Workspace
from stellarweaver.web.isolation import _MAX_BYTES, _MAX_FILES, DockerRunner, Isolation
from stellarweaver.web.snapshots import (
    ARCHIVE_LIMIT,
    SnapshotCache,
    SnapshotMismatch,
    verify_transfer,
)


def _validate_archive(archive: BinaryIO) -> None:
    archive.seek(0)
    paths: set[str] = set()
    size = 0
    with tarfile.open(fileobj=archive, mode="r:") as entries:
        for entry in entries:
            path = PurePosixPath(entry.name)
            if (
                not entry.isfile()
                or entry.sparse
                or not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.name
                or ":" in entry.name
                or entry.name != path.as_posix()
                or Workspace.is_protected(entry.name)
                or path.as_posix() in paths
                or entry.size < 0
            ):
                raise ValueError("Invalid snapshot entry")
            paths.add(path.as_posix())
            size += entry.size
            if size > _MAX_BYTES or len(paths) > _MAX_FILES:
                raise ValueError("Snapshot exceeded workspace limit")
    archive.seek(0)


@dataclass
class Job:
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[JSONDict] | None = None
    cancelled: bool = False
    cleanup_confirmed: bool = False

    def cancel(self) -> None:
        if not self.cancelled:
            self.cancelled = True
            if self.task is not None and not self.task.done():
                self.task.cancel()


def create_broker(isolation: Isolation, token: str, slots: int = 2) -> FastAPI:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) or not 1 <= slots <= 8:
        raise ValueError("Configure a private runner token and between one and eight slots")
    active: dict[UUID, Job] = {}
    snapshots = SnapshotCache()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(isolation.reap)
        try:
            yield
        finally:
            pending = tuple(active.values())
            for job in pending:
                job.cancel()
            await asyncio.gather(*(job.task for job in pending if job.task), return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: str | None) -> None:
        if (
            not authorization
            or not authorization.isascii()
            or not secrets.compare_digest(authorization, "Bearer " + token)
        ):
            raise HTTPException(401, "Executor credentials required")

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/verify/{job_id}")
    async def verify(
        job_id: UUID,
        request: Request,
        timeout_seconds: Annotated[float, Query(alias="timeout", gt=0, le=300)],
        command: Annotated[int | None, Query(ge=0)] = None,
        action: Action | None = None,
        target: str | None = None,
        framework: str | None = None,
        metrics: bool = False,
        snapshot: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
        base: Annotated[str | None, Query(pattern=r"^[a-f0-9]{64}$")] = None,
        scope: Annotated[str | None, Query(pattern=r"^[a-f0-9]{32}$")] = None,
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONDict:
        authorize(authorization)
        if (snapshot is None) != (scope is None) or (base is not None and snapshot is None):
            raise HTTPException(422, "Incomplete snapshot metadata")
        if (
            command is not None
            and command < len(isolation.commands)
            and action is None
            and target is None
            and framework is None
        ):
            argv = list(isolation.commands[command])
        elif command is None and action is not None and isolation.development:
            try:
                args = DevelopmentArgs.model_validate({"target": target, "framework": framework})
            except ValueError as exc:
                raise HTTPException(422, "Invalid development target") from exc
            argv = development_argv(action, args)
        else:
            raise HTTPException(422, "Unknown verification command")
        if job_id in active:
            raise HTTPException(409, "Verification already active")
        if len(active) >= slots:
            raise HTTPException(429, "Executor is at capacity")
        job = active[job_id] = Job()
        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
                # Docker reads the file descriptor directly after the validation pass.
                (Path(directory) / "source.tar").open("w+b", buffering=0) as archive,
            ):
                async with asyncio.timeout(30):
                    async for chunk in request.stream():
                        if job.cancelled:
                            raise HTTPException(409, "Verification cancelled")
                        if archive.tell() + len(chunk) > ARCHIVE_LIMIT:
                            raise HTTPException(413, "Snapshot too large")
                        archive.write(chunk)
                runnable: BinaryIO = archive
                if snapshot is not None and scope is not None:
                    cache_scope, expected = scope, snapshot
                    runnable = stack.enter_context((Path(directory) / "merged.tar").open("w+b"))

                    def materialize() -> None:
                        previous = snapshots.get(cache_scope, base) if base is not None else None
                        verify_transfer(
                            archive, runnable, expected, baseline=previous, base_digest=base
                        )
                        snapshots.put(cache_scope, expected, runnable.read(ARCHIVE_LIMIT + 1))
                        runnable.seek(0)

                    task_validation = asyncio.to_thread(materialize)
                else:
                    task_validation = asyncio.to_thread(_validate_archive, archive)
                validation = asyncio.create_task(task_validation)
                try:
                    await asyncio.shield(validation)
                except asyncio.CancelledError:
                    await _wait_cleanup(validation)
                    raise
                if job.cancelled or await request.is_disconnected():
                    raise HTTPException(409, "Verification cancelled")
                runner = DockerRunner(Path(directory), isolation, pipeline_metrics=metrics)
                task = job.task = asyncio.create_task(
                    runner.run_archive(
                        runnable,
                        argv,
                        config=ExecutionConfig(max_output_bytes_per_stream=65536),
                        timeout_seconds=timeout_seconds,
                    )
                )
                try:
                    while not task.done():
                        if await request.is_disconnected():
                            job.cancel()
                            break
                        await asyncio.wait({task}, timeout=0.15)
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    job.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                job.cleanup_confirmed = bool(
                    result["data"]["process_management"]["cleanup_confirmed"]
                )
                if snapshot is not None:
                    result["data"]["snapshot_manifest_sha256"] = snapshot
                return result
        except SnapshotMismatch as exc:
            raise HTTPException(412 if base is not None else 422, "Snapshot mismatch") from exc
        except (ValueError, tarfile.TarError) as exc:
            raise HTTPException(422, "Invalid snapshot") from exc
        except ToolError as exc:
            raise HTTPException(503, exc.code) from exc
        except TimeoutError as exc:
            raise HTTPException(408, "Snapshot upload timed out") from exc
        finally:
            job.done.set()
            active.pop(job_id, None)

    @app.delete("/verify/{job_id}")
    async def cancel(
        job_id: UUID,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        authorize(authorization)
        job = active.get(job_id)
        if job is None:
            return {"cleanup_confirmed": False}
        job.cancel()
        try:
            await asyncio.wait_for(job.done.wait(), timeout=20)
        except TimeoutError:
            return {"cleanup_confirmed": False}
        return {"cleanup_confirmed": job.cleanup_confirmed}

    return app


def main() -> None:
    isolation = Isolation.from_environment()
    if isolation is None:
        raise SystemExit("Configure an executor image")
    app = create_broker(
        isolation,
        os.getenv("SW_WEB_EXECUTOR_TOKEN", ""),
        int(os.getenv("SW_WEB_EXECUTOR_SLOTS", "2")),
    )
    uvicorn.run(app, host="0.0.0.0", port=8766, proxy_headers=False, timeout_graceful_shutdown=25)


if __name__ == "__main__":
    main()
