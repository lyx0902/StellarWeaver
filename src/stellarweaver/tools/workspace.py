"""Workspace path and content boundaries shared by every built-in tool."""

from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
from typing import Any

import pathspec

MAX_EDIT_BYTES = 1024 * 1024
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".stellarweaver",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)


class ToolError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_data(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": False,
            "details": self.details,
        }


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be a directory")
        rules: list[str] = []
        for name in (".gitignore", ".stellarweaverignore"):
            candidate = self.root / name
            if candidate.is_file() and not candidate.is_symlink():
                rules.extend(candidate.read_text(encoding="utf-8").splitlines())
        self._ignore = pathspec.GitIgnoreSpec.from_lines(rules)
        self.protected_roots: tuple[Path, ...] = ()

    def protect(self, path: Path) -> None:
        """Freeze a runtime-owned location against all file tools and enumeration."""
        resolved = path.resolve(strict=False)
        if resolved not in self.protected_roots:
            self.protected_roots = (*self.protected_roots, resolved)

    def _is_extra_protected(self, path: Path) -> bool:
        return any(path == root or path.is_relative_to(root) for root in self.protected_roots)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def is_protected(relative: str) -> bool:
        parts = Path(relative).parts
        for part in parts:
            name = part.lower()
            if name in {
                ".git",
                ".stellarweaver",
                ".aws",
                ".ssh",
                "credentials",
                "credentials.json",
                "session.db",
                "sessions.db",
            }:
                return True
            if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
                return True
            if (
                name.endswith(
                    (
                        ".pem",
                        ".key",
                        ".p12",
                        ".pfx",
                        ".sqlite",
                        ".sqlite3",
                        ".sqlite-wal",
                        ".sqlite-shm",
                        ".sqlite3-wal",
                        ".sqlite3-shm",
                    )
                )
                or name.startswith("id_rsa")
                or name.startswith("id_ed25519")
            ):
                return True
        return False

    def resolve(
        self, value: str, *, allow_directory: bool = False, must_exist: bool = True
    ) -> Path:
        supplied = Path(value)
        if (
            supplied.is_absolute()
            or PureWindowsPath(value).is_absolute()
            or ".." in supplied.parts
            or ".." in PureWindowsPath(value).parts
        ):
            raise ToolError(
                "PATH_OUTSIDE_WORKSPACE",
                "Use a relative path inside the fixed workspace.",
                {"path": value},
            )
        if any(":" in part for part in supplied.parts):
            raise ToolError(
                "INVALID_PATH",
                "Alternate streams and device paths are not supported.",
                {"path": value},
            )
        if PureWindowsPath(value).is_reserved():
            raise ToolError(
                "INVALID_PATH", "Reserved Windows device paths are not supported.", {"path": value}
            )
        if self.is_protected(value):
            raise ToolError(
                "PROTECTED_PATH", "This path is protected from built-in tools.", {"path": value}
            )
        try:
            candidate = (self.root / supplied).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ToolError("INVALID_PATH", str(exc), {"path": value}) from exc
        if not candidate.is_relative_to(self.root):
            raise ToolError(
                "PATH_OUTSIDE_WORKSPACE", "The resolved path leaves the workspace.", {"path": value}
            )
        if self.is_protected(self.relative(candidate)) or self._is_extra_protected(candidate):
            raise ToolError("PROTECTED_PATH", "The resolved path is protected.", {"path": value})
        if must_exist and not candidate.exists():
            raise ToolError("FILE_NOT_FOUND", "Path does not exist.", {"path": value})
        if not allow_directory and candidate.is_dir():
            raise ToolError(
                "IS_DIRECTORY", "Expected a file, received a directory.", {"path": value}
            )
        return candidate

    def is_ignored(self, relative: str, *, is_dir: bool = False) -> bool:
        if self.is_protected(relative) or self._is_extra_protected(
            (self.root / relative).resolve(strict=False)
        ):
            return True
        if any(part in SKIP_DIRECTORIES for part in Path(relative).parts):
            return True
        return bool(self._ignore.match_file(relative + "/" if is_dir else relative))
