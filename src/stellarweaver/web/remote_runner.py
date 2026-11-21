"""Worker-side client for the private G5 executor."""

import asyncio
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx

from stellarweaver.config import ExecutionConfig
from stellarweaver.domain import JSONDict
from stellarweaver.tools.commands import _wait_cleanup
from stellarweaver.tools.development import decode_development
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web.isolation import Isolation, _archive


@dataclass(frozen=True)
class RemoteRunner:
    root: Path
    isolation: Isolation
    endpoint: str
    token: str = field(repr=False)

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
        if development is not None:
            action, args = development
            params.update(action=action, target=args.target, framework=args.framework)
        else:
            params["command"] = self.isolation.commands.index(tuple(argv))
        with (
            tempfile.TemporaryDirectory() as directory,
            (Path(directory) / "source.tar").open("w+b") as archive,
        ):
            snapshot = asyncio.create_task(asyncio.to_thread(_archive, self.root, archive))
            try:
                await asyncio.shield(snapshot)
            except asyncio.CancelledError:
                await _wait_cleanup(snapshot)
                raise

            async def chunks() -> AsyncIterator[bytes]:
                while chunk := archive.read(65536):
                    yield chunk

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout + 55, connect=5),
                headers={"Authorization": "Bearer " + self.token},
                trust_env=False,
            ) as client:
                try:
                    response = await client.post(
                        key,
                        params=params,
                        content=chunks(),
                        headers={"Content-Type": "application/x-tar"},
                    )
                    response.raise_for_status()
                    result: JSONDict = response.json()
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
