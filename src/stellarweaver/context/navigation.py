"""Bounded Python definitions, approximate references and related tests."""

from __future__ import annotations

import ast
import io
import os
import re
import stat
import time
import tokenize
import unicodedata
from collections import OrderedDict
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from threading import Lock
from typing import Literal

from stellarweaver.context.repo_map import RepoMap, direct_entry
from stellarweaver.domain import JSONDict
from stellarweaver.tools.workspace import ToolError, sha256_bytes

_PATH = re.compile(r"(?<![\w./\\:-])(?:[\w.-]+[/\\])*[\w-]+\.py\b")
_FRAME = re.compile(r'File ["\']([^"\']+\.py)["\'], line (\d+)(?:, in (\w+))?')
_SYMBOL = re.compile(r"`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`|\b([A-Za-z_]\w*)\(")
_ERROR = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b")
_TEST = re.compile(r"\btest_\w+\b")
NavigationMode = Literal["find_symbol", "find_references", "find_related_tests"]


@dataclass(frozen=True)
class IssueSignals:
    paths: tuple[str, ...]
    symbols: tuple[str, ...]
    errors: tuple[str, ...]
    frames: tuple[tuple[str, int], ...]

    @staticmethod
    def extract(text: str) -> IssueSignals:
        text = text[:65536]
        frames = tuple(
            (match[1].replace("\\", "/"), int(match[2]))
            for match in _FRAME.finditer(text)
            if len(match[2]) <= 9
        )[:20]
        paths = tuple(dict.fromkeys(path.replace("\\", "/") for path in _PATH.findall(text)))[:30]
        names = [match[1] or match[2] for match in _SYMBOL.finditer(text)]
        names.extend(match[3] for match in _FRAME.finditer(text) if match[3])
        names.extend(_TEST.findall(text))
        return IssueSignals(
            paths,
            tuple(dict.fromkeys(names))[:30],
            tuple(dict.fromkeys(_ERROR.findall(text)))[:20],
            frames,
        )


@dataclass(frozen=True)
class Hit:
    symbol: str
    line: int
    kind: str


@dataclass(frozen=True)
class Module:
    sha256: str
    module: str
    hits: tuple[Hit, ...]
    incomplete: bool


def _module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if len(parts) > 1 and parts[0] == "src":
        parts.pop(0)
    if parts[-1:] == ["__init__"]:
        parts.pop()
    return ".".join(parts)


class _Index(ast.NodeVisitor):
    def __init__(self, module: str, package: str) -> None:
        self.module = module
        self.package = package
        self.scope: list[str] = []
        self.symbols: list[Hit] = []
        self.references: list[Hit] = []

    def _definition(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        symbol = ".".join(part for part in (self.module, *self.scope) if part)
        self.symbols.append(Hit(symbol, node.lineno, "definition"))
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = _definition
    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition

    def visit_Import(self, node: ast.Import) -> None:
        self.references.extend(Hit(alias.name, node.lineno, "import") for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        package = self.package.split(".") if self.package else []
        scope = (
            package[: len(package) - node.level + 1]
            if node.level and node.level <= len(package)
            else []
        )
        prefix = ".".join(part for part in (*scope, node.module) if part)
        for alias in node.names:
            qualified = ".".join(part for part in (prefix, alias.name) if part)
            self.references.append(Hit(qualified, node.lineno, "import"))
            if alias.name != "*" and not self.scope:
                name = ".".join(part for part in (self.module, alias.asname or alias.name) if part)
                self.symbols.append(Hit(name, node.lineno, "import_binding"))

    def visit_Name(self, _: ast.Name) -> None:
        # Unqualified names are collected after the import declarations.
        return


def _index(path: str, raw: bytes, digest: str) -> Module:
    module = _module_name(path)
    package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
    try:
        tree = ast.parse(raw, filename=path)
        index = _Index(module, package)
        index.visit(tree)
        # Only resolve aliases with unique top-level imports. All hits are syntactic observations.
        alias_targets: dict[str, set[str]] = {}
        imports = {
            node.lineno: node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)
        }
        for reference in index.references:
            statement = imports.get(reference.line)
            if statement is None:
                continue
            for alias in statement.names:
                if reference.symbol.endswith("." + alias.name) or reference.symbol == alias.name:
                    head = alias.asname or alias.name.split(".")[0]
                    qualified = (
                        reference.symbol
                        if alias.asname or not isinstance(statement, ast.Import)
                        else head
                    )
                    alias_targets.setdefault(head, set()).add(qualified)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                index.references.append(Hit(node.id, node.lineno, "name"))
                values = alias_targets.get(node.id, set())
                if len(values) == 1:
                    index.references.append(Hit(next(iter(values)), node.lineno, "import_alias"))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                attributes = [node.attr]
                base = node.value
                while isinstance(base, ast.Attribute):
                    attributes.append(base.attr)
                    base = base.value
                if isinstance(base, ast.Call) and isinstance(base.func, ast.Name):
                    base = base.func
                if isinstance(base, ast.Name):
                    aliases = alias_targets.get(base.id, set())
                    head = next(iter(aliases)) if len(aliases) == 1 else base.id
                    index.references.append(
                        Hit(".".join([head, *reversed(attributes)]), node.lineno, "attribute")
                    )
        hits = tuple(dict.fromkeys([*index.symbols, *index.references]))
        return Module(digest, module, hits[:1500], len(hits) > 1500)
    except (ValueError, SyntaxError, RecursionError):
        return Module(digest, module, (), True)


def _mentions(source: bytes, needle: bytes, leaf: str) -> bool:
    if needle in source:
        return True
    if source.isascii():
        return False
    # Python normalizes Unicode identifiers before reporting AST names.
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
        text = source.decode(encoding)
    except (LookupError, SyntaxError, UnicodeError):
        return True
    return leaf in unicodedata.normalize("NFKC", text)


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_mtime_ns,
        info.st_size,
        info.st_ctime_ns,
        info.st_dev,
        info.st_ino,
        info.st_mode,
    )


class NavigationSources:
    """Share verified source bytes between queries without sharing mutable AST results."""

    def __init__(self, max_files: int, max_bytes: int = 16 * 1024 * 1024) -> None:
        self.max_files, self.max_bytes = max_files, max_bytes
        self.size = 0
        self.entries: OrderedDict[str, tuple[tuple[int, ...], bytes]] = OrderedDict()
        self.lock = Lock()

    def get(self, path: str, stamp: tuple[int, ...]) -> bytes | None:
        with self.lock:
            entry = self.entries.get(path)
            if entry is not None and stamp == entry[0]:
                self.entries.move_to_end(path)
                return entry[1]
            return None

    def put(self, path: str, stamp: tuple[int, ...], data: bytes) -> None:
        if len(data) > self.max_bytes:
            return
        with self.lock:
            old = self.entries.pop(path, None)
            self.size += len(data) - (len(old[1]) if old else 0)
            self.entries[path] = (stamp, data)
            while len(self.entries) > self.max_files or self.size > self.max_bytes:
                _, entry = self.entries.popitem(last=False)
                self.size -= len(entry[1])


class NavigationIndex:
    def __init__(
        self, repo: RepoMap, *, prefilter: bool = False, sources: NavigationSources | None = None
    ) -> None:
        self.repo = repo
        self.prefilter = prefilter
        self.cache: dict[str, Module] = {}
        self.sources = (sources or NavigationSources(repo.max_files)) if prefilter else None

    def _source(self, path: str) -> bytes | None:
        workspace = self.repo.workspace
        try:
            target = workspace.resolve(path)
            if workspace.relative(target) != path or workspace.is_ignored(path):
                return None
            with target.open("rb") as source:
                raw = source.read(self.repo.max_file_bytes + 1)
            if len(raw) > self.repo.max_file_bytes:
                return None
        except (OSError, ToolError, ValueError):
            return None
        return raw

    def _cached_source(self, name: str) -> tuple[bytes | None, bool]:
        assert self.sources is not None
        path = self.repo.workspace.root / name
        try:
            info = path.lstat()
            if (
                not direct_entry(info)
                or not stat.S_ISREG(info.st_mode)
                or info.st_size > self.repo.max_file_bytes
            ):
                return None, False
            key = _stamp(info)
            cached = self.sources.get(name, key)
            if cached is not None:
                return cached, True
            data = self._source(name)
            current = path.lstat()
            if data is not None and direct_entry(current) and _stamp(current) == key:
                self.sources.put(name, key, data)
                return data, False
        except (OSError, ValueError):
            pass
        return None, False

    def _file(self, path: str, raw: bytes | None = None) -> Module | None:
        if raw is None:
            raw = self._source(path)
        if raw is None:
            return None
        digest = sha256_bytes(raw)
        cached = self.cache.get(path)
        if cached is None or cached.sha256 != digest:
            cached = self.cache[path] = _index(path, raw, digest)
        return cached

    def query(self, mode: NavigationMode, name: str, path_hint: str | None, limit: int) -> JSONDict:
        started = time.monotonic()
        paths, truncated = self.repo.paths(direct=self.prefilter)
        leaf = name.rsplit(".", 1)[-1]
        candidates = [path for path in paths if path.endswith(".py")]
        candidates.sort(
            key=lambda path: (
                path != path_hint,
                mode == "find_related_tests" and not is_test(path),
                leaf.casefold() not in path.casefold(),
                path,
            )
        )
        selected = candidates[: self.repo.max_indexed_files]
        index_truncated = len(candidates) > len(selected)
        unavailable = 0
        sources: dict[str, bytes] = {}
        prefilter_details: JSONDict = {}
        if self.prefilter and len(candidates) > self.repo.max_indexed_files:
            needle = leaf.encode("utf-8", errors="surrogatepass")
            scanned = hits = reused = 0
            index_truncated = False
            for path in candidates:
                if mode == "find_related_tests" and not is_test(path):
                    continue
                scanned += 1
                source, from_cache = self._cached_source(path)
                reused += int(from_cache)
                if source is None:
                    unavailable += 1
                    continue
                found = _mentions(source, needle, leaf)
                hits += int(found)
                if found or path == path_hint:
                    if len(sources) == self.repo.max_indexed_files:
                        index_truncated = True
                        break
                    sources[path] = source
            selected = list(sources)
            prefilter_details = {
                "prefilter_scanned": scanned,
                "prefilter_matches": hits,
                "prefilter_cache_hits": reused,
            }
        self.cache = {path: module for path, module in self.cache.items() if path in selected}
        rows = []
        for path in selected:
            if mode == "find_related_tests" and not is_test(path):
                continue
            source = sources.get(path)
            if source is None and self.sources is not None:
                source, _ = self._cached_source(path)
                if source is None:
                    unavailable += 1
                    continue
            entry = self._file(path, source)
            if entry is None:
                unavailable += 1
                continue
            unavailable += int(entry.incomplete)
            for hit in entry.hits:
                definition = hit.kind in {"definition", "import_binding"}
                if (mode == "find_symbol") != definition:
                    continue
                exact = hit.symbol == name
                if not exact and not hit.symbol.endswith("." + name):
                    if "." in name or hit.symbol.rsplit(".", 1)[-1] != leaf:
                        continue
                rank = (
                    int(exact),
                    int(path == path_hint),
                    int(hit.kind not in {"name", "import_binding"}),
                )
                rows.append((rank, path, hit, entry.sha256))
        rows.sort(key=lambda row: (tuple(-i for i in row[0]), row[1], row[2].line, row[2].symbol))
        if mode == "find_related_tests":
            unique: dict[str, tuple[tuple[int, int, int], str, Hit, str]] = {}
            for row in rows:
                unique.setdefault(row[1], row)
            rows = list(unique.values())
        return {
            "matches": [
                {
                    **asdict(hit),
                    "path": path,
                    "sha256": digest,
                    "why": "exact" if rank[0] else "suffix",
                }
                for rank, path, hit, digest in rows[:limit]
            ],
            "index_version": "python-ast/1",
            "coverage": "static_approximation",
            "dynamic_resolution": "unknown",
            "indexed_files": len(self.cache),
            "unavailable_or_partial_files": unavailable,
            "truncated": truncated or index_truncated or len(rows) > limit or bool(unavailable),
            "duration_ms": int((time.monotonic() - started) * 1000),
            **prefilter_details,
        }


def is_test(path: str) -> bool:
    item = PurePosixPath(path)
    return item.name.startswith("test_") or item.name.endswith("_test.py") or "tests" in item.parts
