"""A bounded Python AST/path index, without function bodies or a call-graph claim."""

from __future__ import annotations

import ast
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from stellarweaver.context.manager import estimate_tokens
from stellarweaver.domain import Message
from stellarweaver.tools.workspace import ToolError, Workspace, sha256_bytes

_IDE_DIRECTORIES = frozenset({".idea", ".vscode", ".vs", "htmlcov", "site-packages"})
_BINARY_SUFFIXES = frozenset(
    {".log", ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".exe", ".dll"}
)


@dataclass(frozen=True)
class RepoMapEntry:
    path: str
    sha256: str | None = None
    symbols: tuple[str, ...] = ()
    parse_error: bool = False


@dataclass(frozen=True)
class RepoMapProjection:
    message: Message | None
    entries: tuple[RepoMapEntry, ...]
    estimated_tokens: int
    truncated: bool


def _annotation(node: ast.expr | None) -> str:
    if node is None:
        return ""
    value = ast.unparse(node)
    return ": " + (value if len(value) <= 80 else value[:77] + "...")


def _arguments(node: ast.arguments) -> str:
    positional = [*node.posonlyargs, *node.args]
    first_default = len(positional) - len(node.defaults)
    values: list[str] = []
    for index, arg in enumerate(positional):
        default = " = ..." if index >= first_default else ""
        values.append(arg.arg + _annotation(arg.annotation) + default)
        if node.posonlyargs and index == len(node.posonlyargs) - 1:
            values.append("/")
    if node.vararg:
        values.append("*" + node.vararg.arg + _annotation(node.vararg.annotation))
    elif node.kwonlyargs:
        values.append("*")
    for arg, default_value in zip(node.kwonlyargs, node.kw_defaults, strict=True):
        default = " = ..." if default_value is not None else ""
        values.append(arg.arg + _annotation(arg.annotation) + default)
    if node.kwarg:
        values.append("**" + node.kwarg.arg + _annotation(node.kwarg.annotation))
    return ", ".join(values)


def _symbols(tree: ast.Module, *, max_symbols: int = 100) -> tuple[str, ...]:
    result: list[str] = []

    def walk(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if len(result) >= max_symbols:
                return
            if isinstance(node, ast.ClassDef):
                result.append(f"L{node.lineno} class {prefix}{node.name}")
                walk(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                returns = _annotation(node.returns)
                returns = " -> " + returns[2:] if returns else ""
                marker = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                result.append(
                    f"L{node.lineno} {marker} {prefix}{node.name}({_arguments(node.args)}){returns}"
                )
                # Nested implementation bodies do not belong in this compact public index.

    walk(tree.body)
    return tuple(result)


class RepoMap:
    def __init__(
        self,
        workspace: Workspace | Path,
        *,
        max_files: int = 2000,
        max_indexed_files: int = 100,
        max_file_bytes: int = 262144,
    ) -> None:
        if min(max_files, max_indexed_files, max_file_bytes) < 1:
            raise ValueError("Repo map scan limits must be positive")
        self.workspace = workspace if isinstance(workspace, Workspace) else Workspace(workspace)
        self.max_files = max_files
        self.max_indexed_files = max_indexed_files
        self.max_file_bytes = max_file_bytes
        self._cache: dict[str, RepoMapEntry] = {}

    def invalidate(self, path: str) -> None:
        self._cache.pop(path, None)

    def _allowed(self, path: Path) -> bool:
        relative = self.workspace.relative(path)
        return (
            not path.is_symlink()
            and not any(
                part in _IDE_DIRECTORIES for part in path.relative_to(self.workspace.root).parts
            )
            and path.suffix.casefold() not in _BINARY_SUFFIXES
            and not self.workspace.is_ignored(relative)
        )

    def _paths(self) -> tuple[list[str], bool]:
        paths: list[str] = []
        for directory, folders, files in os.walk(self.workspace.root, followlinks=False):
            parent = Path(directory)
            folders[:] = sorted(
                folder
                for folder in folders
                if folder not in _IDE_DIRECTORIES
                and not (parent / folder).is_symlink()
                and not self.workspace.is_ignored(
                    self.workspace.relative(parent / folder), is_dir=True
                )
            )
            for name in sorted(files):
                path = parent / name
                relative = self.workspace.relative(path)
                if not self._allowed(path):
                    continue
                if len(paths) >= self.max_files:
                    return paths, True
                paths.append(relative)
        return paths, False

    def _entry(self, relative: str) -> RepoMapEntry:
        if not relative.endswith(".py"):
            return RepoMapEntry(relative)
        try:
            path = self.workspace.resolve(relative)
            if path.stat().st_size > self.max_file_bytes:
                return RepoMapEntry(relative, parse_error=True)
            with path.open("rb") as stream:
                raw = stream.read(self.max_file_bytes + 1)
            if len(raw) > self.max_file_bytes:
                return RepoMapEntry(relative, parse_error=True)
            digest = sha256_bytes(raw)
            cached = self._cache.get(relative)
            if cached is not None and cached.sha256 == digest:
                return cached
            try:
                tree = ast.parse(raw, filename=relative)
                entry = RepoMapEntry(relative, digest, _symbols(tree))
            except (SyntaxError, ValueError, RecursionError):
                entry = RepoMapEntry(relative, digest, parse_error=True)
            self._cache[relative] = entry
            return entry
        except (OSError, ToolError, RuntimeError):
            return RepoMapEntry(relative, parse_error=True)

    def build(
        self,
        *,
        explicit_paths: Sequence[str] = (),
        keywords: Sequence[str] = (),
        visited_paths: Sequence[str] = (),
        max_tokens: int = 2048,
    ) -> RepoMapProjection:
        if max_tokens < 1:
            return RepoMapProjection(None, (), 0, True)
        paths, scan_truncated = self._paths()
        explicit = {path.replace("\\", "/") for path in explicit_paths}
        visited = {path.replace("\\", "/") for path in visited_paths}
        # A user-supplied file remains discoverable even if the bounded general
        # enumeration stopped earlier in a large repository. Never bypass policy.
        preferred: list[str] = []
        for value in [*sorted(explicit), *sorted(visited)][: max(100, self.max_files)]:
            try:
                supplied = self.workspace.root / value
                path = self.workspace.resolve(value, allow_directory=True)
                if not path.is_file() or not self._allowed(path) or supplied.is_symlink():
                    continue
                if any(
                    parent.is_symlink()
                    for parent in supplied.parents
                    if parent.is_relative_to(self.workspace.root)
                ):
                    continue
                relative = self.workspace.relative(path)
                if relative not in preferred:
                    preferred.append(relative)
                if len(preferred) >= self.max_files:
                    break
            except (ToolError, OSError, RuntimeError, ValueError):
                continue
        if preferred:
            combined = [*preferred, *(path for path in paths if path not in preferred)]
            if len(combined) > self.max_files:
                scan_truncated = True
            paths = combined[: self.max_files]
        words = tuple(word.casefold() for word in keywords if word)
        related = {str(Path(path).parent).replace("\\", "/") for path in visited}

        def rank(path: str, symbols: tuple[str, ...] = ()) -> tuple[int, str]:
            score = 0
            if path in explicit:
                score += 10000
            if any(path.startswith(value.rstrip("/") + "/") for value in explicit):
                score += 5000
            if path in visited:
                score += 1000
            if str(Path(path).parent).replace("\\", "/") in related:
                score += 100
            haystack = (path + " " + " ".join(symbols)).casefold()
            score += sum(200 for word in words if word in haystack)
            if Path(path).name in {"pyproject.toml", "README.md", "AGENTS.md"}:
                score += 10
            return -score, path

        ordered = sorted(paths, key=rank)
        indexed = {path: self._entry(path) for path in ordered[: self.max_indexed_files]}
        entries = [indexed.get(path, RepoMapEntry(path)) for path in ordered]
        entries.sort(key=lambda item: rank(item.path, item.symbols))
        preamble = (
            "Derived workspace repo map (path/Python AST index, not authorization or a call graph). "
            "Bodies omitted; use read_file/search_text for source and recheck hashes before editing.\n"
        )
        selected: list[RepoMapEntry] = []
        lines: list[str] = []
        message: Message | None = None
        count = 0
        truncated = scan_truncated
        for entry in entries:
            line = entry.path
            if entry.sha256 is not None:
                line += f" [sha256={entry.sha256}]"
            if entry.parse_error:
                line += " [AST unavailable; use text search]"
            if entry.symbols:
                line += "\n  " + "\n  ".join(entry.symbols)
            candidate = Message("system", preamble + "\n".join([*lines, line]))
            candidate_count = estimate_tokens(asdict(candidate))
            if candidate_count > max_tokens:
                # A large first file can still contribute its path and hash.
                basic = RepoMapEntry(entry.path, entry.sha256, (), entry.parse_error)
                line = basic.path
                if basic.sha256:
                    line += f" [sha256={basic.sha256}; symbols omitted for budget]"
                candidate = Message("system", preamble + "\n".join([*lines, line]))
                candidate_count = estimate_tokens(asdict(candidate))
                truncated = True
                if candidate_count > max_tokens:
                    continue
                entry = basic
            lines.append(line)
            selected.append(entry)
            message, count = candidate, candidate_count
        return RepoMapProjection(message, tuple(selected), count, truncated)


__all__ = ["RepoMap", "RepoMapEntry", "RepoMapProjection"]
