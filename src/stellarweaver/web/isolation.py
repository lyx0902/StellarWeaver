"""Private Git worktrees and disposable, networkless verification containers."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

from stellarweaver.domain import JSONDict
from stellarweaver.tools.commands import RunCommandArgs, _wait_cleanup, execute_command
from stellarweaver.tools.workspace import ToolError, Workspace

_MAX_FILES = 6000
_MAX_BYTES = 32 * 1024 * 1024
_BOOTSTRAP = """
import os, pathlib, sys, tarfile
root = pathlib.Path("/tmp/work")
root.mkdir(mode=0o700)
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as archive:
    archive.extractall(root, filter="data")
os.chdir(root / sys.argv[1])
os.execvpe(sys.argv[2], sys.argv[2:], {
    "PATH": os.environ["PATH"], "HOME": "/tmp", "TMPDIR": "/tmp",
    "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
})
"""


def git(root: Path, *args: str, patch: bytes | None = None) -> bytes:
    env = {
        **{k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR"}},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_LITERAL_PATHSPECS": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C.UTF-8",
    }
    try:
        proc = subprocess.run(
            [
                "git",
                "--no-pager",
                "-c",
                f"safe.directory={root}",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.autocrlf=false",
                *args,
            ],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL if patch is None else None,
            input=patch,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(
            "ISOLATION_GIT_FAILED", "Cannot execute the required Git operation."
        ) from exc
    if proc.returncode:
        raise ToolError("ISOLATION_GIT_FAILED", "Git rejected the required worktree operation.")
    return proc.stdout


def task_worktree(state: Path, tenant: str, session: str, source: Path) -> Path:
    container = state / "g4" / str(UUID(tenant)) / str(UUID(session))
    work = container / "work"
    if (container / "ready").is_file():
        if work.is_dir() and work.resolve() == work and (work / ".git").is_file():
            return work
        raise ToolError("WORKTREE_UNAVAILABLE", "The saved task worktree is missing.")
    if container.exists():
        shutil.rmtree(container)
    container.mkdir(parents=True, mode=0o700)
    repo = container / "base.git"
    git(container, "init", "--bare", "--template=", str(repo))
    git(
        repo,
        "-c",
        f"safe.directory={source}",
        "fetch",
        "--no-tags",
        "--depth=1",
        "--",
        str(source),
        "HEAD",
    )
    tree = git(repo, "ls-tree", "-r", "-l", "-z", "FETCH_HEAD").split(b"\0")[:-1]
    if len(tree) > _MAX_FILES:
        raise ToolError("WORKTREE_LIMIT", "The repository exceeds the task workspace file limit.")
    size = 0
    for row in tree:
        metadata, name = row.split(b"\t", 1)
        mode, kind, _, blob_size = metadata.split()
        relative = name.decode("utf-8", errors="strict")
        if Workspace.is_protected(relative) or mode not in {b"100644", b"100755"}:
            raise ToolError(
                "WORKTREE_UNSUPPORTED_ENTRY",
                "The base commit contains a protected file, symbolic link or submodule.",
            )
        if kind == b"blob":
            size += int(blob_size)
    if size > _MAX_BYTES:
        raise ToolError("WORKTREE_LIMIT", "The base commit exceeds the task workspace size limit.")
    git(repo, "worktree", "add", "--detach", "--", str(work), "FETCH_HEAD")
    (container / "ready").touch(exist_ok=False)
    return work


def saved_worktree(state: Path, tenant: str, session: str) -> Path:
    container = state / "g4" / str(UUID(tenant)) / str(UUID(session))
    work = container / "work"
    if (container / "ready").is_file() and work.is_dir() and work.resolve() == work:
        return work
    raise ToolError("WORKTREE_UNAVAILABLE", "The saved task worktree is missing.")


def snapshot_paths(root: Path) -> list[tuple[str, Path]]:
    paths = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    git_entries = set(paths.decode("utf-8").split("\0")[:-1])
    if len(git_entries) > _MAX_FILES:
        raise ToolError("WORKTREE_LIMIT", "The test snapshot exceeds the file limit.")
    workspace = Workspace(root)
    size = 0
    files = []
    for relative in sorted(git_entries):
        if Workspace.is_protected(relative):
            continue
        path = root / relative
        if not path.exists() and not path.is_symlink():
            continue
        resolved = workspace.resolve(relative)
        if resolved != path or not path.is_file() or path.stat().st_nlink != 1:
            raise ToolError("SNAPSHOT_UNSAFE_ENTRY", "Test snapshots require regular files.")
        size += path.stat().st_size
        if size > _MAX_BYTES:
            raise ToolError("WORKTREE_LIMIT", "The test snapshot exceeds the size limit.")
        files.append((relative, path))
    return files


def snapshot_hashes(root: Path) -> dict[str, str] | None:
    try:
        return {
            relative: hashlib.sha256(path.read_bytes()).hexdigest()
            for relative, path in snapshot_paths(root)
        }
    except (ToolError, OSError, ValueError):
        return None


def _archive(root: Path, target: BinaryIO) -> None:
    with tarfile.open(fileobj=target, mode="w") as archive:
        for relative, path in snapshot_paths(root):
            info = archive.gettarinfo(str(path), relative)
            with path.open("rb") as source:
                archive.addfile(info, source)
    target.seek(0)


@dataclass(frozen=True)
class Isolation:
    image: str
    commands: tuple[tuple[str, ...], ...]
    namespace: str

    @classmethod
    def from_environment(cls) -> "Isolation | None":
        image = os.getenv("SW_WEB_RUNNER_IMAGE")
        if not image:
            return None
        namespace = os.getenv("SW_WEB_RUNNER_NAMESPACE", "stellarweaver")
        raw = json.loads(os.getenv("SW_WEB_VERIFY_COMMANDS", '[["python","-m","unittest","-q"]]'))
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,62}", namespace):
            raise ValueError("Invalid runner namespace")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./:@-]{0,254}", image):
            raise ValueError("Invalid runner image")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
            raise ValueError("Set one to twelve verification commands as argv arrays")
        commands = tuple(tuple(RunCommandArgs(argv=argv).argv) for argv in raw)
        if len(set(commands)) != len(commands):
            raise ValueError("Verification commands must be unique")
        return cls(image, commands, namespace)

    def reap(self) -> None:
        listed = subprocess.check_output(
            [
                "docker",
                "ps",
                "-aq",
                "--filter",
                f"label=io.stellarweaver.runner={self.namespace}",
            ],
            timeout=15,
        )
        for identifier in listed.decode("ascii").split():
            subprocess.run(
                ["docker", "rm", "-f", identifier],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=15,
            )


@dataclass(frozen=True)
class DockerRunner:
    root: Path
    isolation: Isolation

    async def __call__(
        self,
        argv: list[str],
        cwd: Path,
        *,
        config: Any = None,
        artifacts_dir: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> JSONDict:
        if tuple(argv) not in self.isolation.commands or cwd != self.root:
            raise ToolError("COMMAND_NOT_CONFIGURED", "Select a configured verification command.")
        with (
            tempfile.TemporaryDirectory() as scratch,
            (Path(scratch) / "input.tar").open("w+b") as archive,
        ):
            snapshot = asyncio.create_task(asyncio.to_thread(_archive, self.root, archive))
            try:
                await asyncio.shield(snapshot)
            except asyncio.CancelledError:
                await _wait_cleanup(snapshot)
                raise
            return await self.run_archive(
                archive,
                argv,
                config=config,
                artifacts_dir=artifacts_dir,
                timeout_seconds=timeout_seconds,
            )

    async def run_archive(
        self,
        archive: BinaryIO,
        argv: list[str],
        *,
        config: Any = None,
        artifacts_dir: Path | None = None,
        timeout_seconds: float | None = None,
    ) -> JSONDict:
        if tuple(argv) not in self.isolation.commands:
            raise ToolError("COMMAND_NOT_CONFIGURED", "Select a configured verification command.")
        name = f"sw-g4-{uuid4().hex}"
        image = await execute_command(
            ["docker", "image", "inspect", "--format", "{{json .}}", self.isolation.image],
            self.root.parent,
            timeout_seconds=15,
        )
        try:
            details = json.loads(image["data"]["stdout"]) if image["data"]["exit_code"] == 0 else {}
            image_id = details.get("Id", "")
            volumes = details.get("Config", {}).get("Volumes")
        except (AttributeError, ValueError):
            image_id, volumes = "", None
        if (
            not isinstance(image_id, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            or volumes
        ):
            raise ToolError(
                "RUNNER_IMAGE_UNAVAILABLE",
                "Load a trusted runner image with no declared volumes.",
            )
        try:
            create = asyncio.create_task(
                execute_command(
                    [
                        "docker",
                        "create",
                        "-i",
                        "--pull",
                        "never",
                        "--name",
                        name,
                        "--label",
                        f"io.stellarweaver.runner={self.isolation.namespace}",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "65534:65534",
                        "--workdir",
                        "/tmp",
                        "--no-healthcheck",
                        "--log-driver",
                        "none",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges",
                        "--pids-limit",
                        "128",
                        "--memory",
                        "512m",
                        "--cpus",
                        "1",
                        "--tmpfs",
                        "/tmp:rw,nosuid,nodev,mode=1777,size=128m",
                        "--entrypoint",
                        "python",
                        image_id,
                        "-I",
                        "-c",
                        _BOOTSTRAP,
                        ".",
                        *argv,
                    ],
                    self.root.parent,
                    timeout_seconds=15,
                )
            )
            try:
                created = await asyncio.shield(create)
            except asyncio.CancelledError:
                await _wait_cleanup(create)
                raise
            if created["data"]["exit_code"] != 0:
                raise ToolError("RUNNER_CREATE_FAILED", "Docker could not create the runner.")
            result = await execute_command(
                ["docker", "start", "-a", "-i", name],
                self.root.parent,
                config=config,
                artifacts_dir=artifacts_dir,
                timeout_seconds=timeout_seconds,
                stdin_file=archive,
            )
        finally:
            cleanup = await _wait_cleanup(self._remove(name))
        result["data"].update(
            argv=argv,
            cwd=".",
            process_cleanup="docker",
            process_management={"backend": "docker", "cleanup_confirmed": cleanup},
        )
        if not cleanup:
            result["status"] = "unknown"
            result["data"]["code"] = "RUNNER_CLEANUP_UNCONFIRMED"
        return result

    async def _remove(self, name: str) -> bool:
        result = await execute_command(
            ["docker", "rm", "-f", name], self.root.parent, timeout_seconds=15
        )
        return result["data"]["exit_code"] == 0 or "No such container" in result["data"]["stderr"]
