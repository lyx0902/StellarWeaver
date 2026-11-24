"""Worker-side client for the private G5 executor."""

import asyncio
import base64
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx

from stellarweaver.config import ExecutionConfig
from stellarweaver.domain import JSONDict
from stellarweaver.tools.commands import _wait_cleanup
from stellarweaver.tools.development import DevelopmentArgs, SnippetArgs, decode_development
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web.isolation import Isolation, _archive, snapshot_paths
from stellarweaver.web.snapshots import SnapshotState, Transfer, pack_transfer


@dataclass(frozen=True)
class RemoteRunner:
    root: Path
    isolation: Isolation
    endpoint: str
    token: str = field(repr=False)
    pipeline_metrics: bool = False
    incremental_snapshots: bool = False
    snapshots: SnapshotState = field(default_factory=SnapshotState, repr=False, compare=False)

    async def __call__(
        self,
        argv: list[str],
        cwd: Path,
        *,
        config: ExecutionConfig | None = None,
        artifacts_dir: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> JSONDict:
        development = decode_development(argv) if self.isolation.development else None
        if cwd != self.root or (development is None and tuple(argv) not in self.isolation.commands):
            raise ToolError("COMMAND_NOT_CONFIGURED", "Select a configured verification command.")
        timeout = timeout_seconds or (config or ExecutionConfig()).default_timeout_seconds
        key = self.endpoint.rstrip("/") + "/verify/" + str(uuid4())
        params: JSONDict = {"timeout": timeout}
        if self.pipeline_metrics:
            params["metrics"] = "true"
        headers: dict[str, str] = {}
        if development is not None:
            action, args = development
            if isinstance(args, SnippetArgs):
                params.update(action=action)
                headers["X-StellarWeaver-Snippet"] = base64.b64encode(
                    args.code.encode("utf-8")
                ).decode("ascii")
            else:
                assert isinstance(args, DevelopmentArgs)
                params.update(action=action, target=args.target, framework=args.framework)
                if args.selector is not None:
                    params["selector"] = args.selector
                if args.keyword is not None:
                    params["keyword"] = args.keyword
        else:
            params["command"] = self.isolation.commands.index(tuple(argv))
        with (
            tempfile.TemporaryDirectory() as directory,
            (Path(directory) / "source.tar").open("w+b") as archive,
        ):
            started = perf_counter()

            def pack(previous: Transfer | None) -> Transfer | None:
                if self.incremental_snapshots:
                    return pack_transfer(self.root, snapshot_paths(self.root), archive, previous)
                _archive(self.root, archive)
                return None

            async def prepare(previous: Transfer | None) -> Transfer | None:
                snapshot = asyncio.create_task(asyncio.to_thread(pack, previous))
                try:
                    return await asyncio.shield(snapshot)
                except asyncio.CancelledError:
                    await _wait_cleanup(snapshot)
                    raise

            transfer = await prepare(self.snapshots.previous)
            packed = perf_counter()

            async def chunks() -> AsyncIterator[bytes]:
                while chunk := archive.read(65536):
                    yield chunk

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout + 55, connect=5),
                headers={"Authorization": "Bearer " + self.token},
                trust_env=False,
            ) as client:
                try:

                    async def upload(item: Transfer | None) -> httpx.Response:
                        query = dict(params)
                        if item:
                            query.update(snapshot=item.digest, scope=self.snapshots.scope)
                            if item.base:
                                query["base"] = item.base
                        return await client.post(
                            key,
                            params=query,
                            content=chunks(),
                            headers={"Content-Type": "application/x-tar", **headers},
                        )

                    response = await upload(transfer)
                    failed_bytes = 0
                    if response.status_code == 412 and transfer is not None and transfer.base:
                        failed_bytes = transfer.size
                        transfer = await prepare(None)
                        response = await upload(transfer)
                    response.raise_for_status()
                    result: JSONDict = response.json()
                    if transfer is not None:
                        self.snapshots.previous = (
                            transfer
                            if result["data"].get("snapshot_manifest_sha256") == transfer.digest
                            else None
                        )
                    if self.pipeline_metrics:
                        timings = result["data"].setdefault("sandbox_timing_ms", {})
                        timings.update(
                            pack=(packed - started) * 1000,
                            executor_round_trip=(perf_counter() - packed) * 1000,
                        )
                        if transfer is not None:
                            timings.update(transfer.metrics())
                            timings["transfer_bytes"] += failed_bytes
                    return result
                except asyncio.CancelledError:
                    confirmed = await _wait_cleanup(self._cancel(client, key))
                    return self._unavailable(argv, cancelled=True, confirmed=confirmed)
                except (httpx.HTTPError, ValueError):
                    return self._unavailable(argv)

    async def _cancel(self, client: httpx.AsyncClient, key: str) -> bool:
        try:
            response = await client.delete(key, timeout=23)
            return response.status_code == 200 and response.json().get("cleanup_confirmed") is True
        except (httpx.HTTPError, ValueError):
            return False

    @staticmethod
    def _unavailable(
        argv: list[str], *, cancelled: bool = False, confirmed: bool = False
    ) -> JSONDict:
        return {
            "status": "cancelled" if cancelled and confirmed else "unknown",
            "content": "Verification executor did not return a confirmed result.",
            "data": {
                "argv": argv,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "code": "COMMAND_CANCELLED" if confirmed else "RUNNER_CLEANUP_UNCONFIRMED",
                "process_management": {"backend": "remote-docker", "cleanup_confirmed": confirmed},
            },
        }
