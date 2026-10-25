"""A source-hash-indexed Python map remains bounded and omits implementation bodies."""

from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
from unittest.mock import patch

from stellarweaver.context.repo_map import RepoMap
from stellarweaver.tools.workspace import Workspace


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_python_symbols_have_paths_lines_signatures_and_hashes_without_bodies(
    tmp_path: Path,
) -> None:
    source = (
        "class Parser:\n"
        "    def parse(self, text: str, /, *, limit: int = 5) -> int:\n"
        "        secret_implementation_body = 123\n"
        "        return secret_implementation_body\n"
        "\n"
        "async def fetch(url: str, *args: str, **kwargs: int) -> str:\n"
        "    return 'not in index'\n"
    )
    write(tmp_path / "中文/parser.py", source)
    projection = RepoMap(tmp_path).build(max_tokens=8000)
    assert projection.message is not None
    text = projection.message.content
    assert projection.message.role == "system"
    assert "中文/parser.py" in text
    assert "L1 class Parser" in text
    assert "L2 def Parser.parse(self, text: str, /, *, limit: int = ...) -> int" in text
    assert "L6 async def fetch(url: str, *args: str, **kwargs: int) -> str" in text
    assert hashlib.sha256((tmp_path / "中文/parser.py").read_bytes()).hexdigest() in text
    assert "secret_implementation_body" not in text
    assert "not in index" not in text


def test_content_hash_cache_survives_equal_metadata_and_invalidates_on_edit(tmp_path: Path) -> None:
    path = tmp_path / "中文.py"
    write(path, "def aaa():\n    pass\n")
    repo_map = RepoMap(tmp_path)
    original_stat = path.stat()
    with patch("stellarweaver.context.repo_map.ast.parse", wraps=ast.parse) as parse:
        first = repo_map.build(max_tokens=8000)
        second = repo_map.build(max_tokens=8000)
        assert parse.call_count == 1
        assert first == second
        write(path, "def bbb():\n    pass\n")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        third = repo_map.build(max_tokens=8000)
        assert parse.call_count == 2
        assert third.entries[0].sha256 != first.entries[0].sha256
        assert "bbb" in third.message.content  # type: ignore[union-attr]
        assert "aaa" not in third.message.content  # type: ignore[union-attr]


def test_syntax_error_keeps_path_and_supports_text_search_fallback(tmp_path: Path) -> None:
    write(tmp_path / "broken.py", "def broken(:\n")
    projection = RepoMap(tmp_path).build(max_tokens=8000)
    assert projection.entries[0].parse_error
    assert projection.entries[0].sha256
    assert "broken.py" in projection.message.content  # type: ignore[union-attr]
    assert "use text search" in projection.message.content  # type: ignore[union-attr]


def test_explicit_keyword_and_visited_file_relationship_order(tmp_path: Path) -> None:
    write(tmp_path / "z.py", "def explicit():\n    pass\n")
    write(tmp_path / "a.py", "def keyword_symbol():\n    pass\n")
    write(tmp_path / "folder/visited.py", "x = 1")
    write(tmp_path / "folder/related.py", "x = 2")
    write(tmp_path / "ordinary.py", "x = 3")
    projection = RepoMap(tmp_path).build(
        explicit_paths=["z.py"],
        keywords=["keyword_symbol"],
        visited_paths=["folder/visited.py"],
        max_tokens=12000,
    )
    assert [entry.path for entry in projection.entries] == [
        "z.py",
        "folder/visited.py",
        "a.py",
        "folder/related.py",
        "ordinary.py",
    ]


def test_sensitive_ignored_dependency_binary_ide_and_runtime_paths_are_excluded(
    tmp_path: Path,
) -> None:
    write(tmp_path / ".gitignore", "ignored/\n")
    for path in [
        "good.py",
        ".env",
        ".aws/credentials",
        ".git/config",
        ".venv/lib.py",
        "node_modules/module.py",
        ".idea/workspace.xml",
        ".vscode/settings.json",
        "ignored/a.py",
        "state/journal.py",
        "output.log",
        "picture.png",
    ]:
        write(tmp_path / path, "data")
    workspace = Workspace(tmp_path)
    workspace.protect(tmp_path / "state")
    projection = RepoMap(workspace).build(max_tokens=10000)
    assert {entry.path for entry in projection.entries} == {"good.py", ".gitignore"}


def test_non_python_paths_are_indexed_without_reading_contents(tmp_path: Path) -> None:
    write(tmp_path / "config.toml", "secret_marker = 'not for map'")
    projection = RepoMap(tmp_path).build(max_tokens=10000)
    assert projection.entries[0].path == "config.toml"
    assert projection.entries[0].sha256 is None
    assert "not for map" not in projection.message.content  # type: ignore[union-attr]


def test_byte_estimated_budget_limits_map_even_for_single_large_symbol_set(tmp_path: Path) -> None:
    write(
        tmp_path / "large.py",
        "\n".join(f"def function_{index}():\n    pass" for index in range(100)),
    )
    projection = RepoMap(tmp_path).build(max_tokens=450)
    assert projection.estimated_tokens <= 450
    assert projection.truncated
    assert projection.message is not None
    assert "large.py" in projection.message.content
    assert projection.entries[0].symbols == ()
    assert RepoMap(tmp_path).build(max_tokens=1).message is None


def test_scan_and_parse_file_limits_are_real_bounds(tmp_path: Path) -> None:
    for index in range(10):
        write(tmp_path / f"{index}.py", f"def f{index}():\n    pass")
    with patch("stellarweaver.context.repo_map.ast.parse", wraps=ast.parse) as parse:
        projection = RepoMap(tmp_path, max_files=4, max_indexed_files=2).build(max_tokens=12000)
        assert len(projection.entries) == 4
        assert parse.call_count == 2
        assert projection.truncated


def test_oversize_python_keeps_path_without_parsing_file(tmp_path: Path) -> None:
    write(tmp_path / "large.py", "x = 1\n" * 100)
    with patch("stellarweaver.context.repo_map.ast.parse", wraps=ast.parse) as parse:
        projection = RepoMap(tmp_path, max_file_bytes=20).build(max_tokens=2000)
        assert parse.call_count == 0
        assert projection.entries[0].path == "large.py"
        assert projection.entries[0].parse_error


def test_explicit_file_beyond_general_scan_limit_is_prioritized_without_policy_bypass(
    tmp_path: Path,
) -> None:
    for index in range(10):
        write(tmp_path / f"{index}.py", f"def f{index}():\n    pass")
    write(tmp_path / ".env", "never include")
    projection = RepoMap(tmp_path, max_files=2, max_indexed_files=2).build(
        explicit_paths=["9.py", ".env", "../outside.py"], max_tokens=4000
    )
    assert projection.entries[0].path == "9.py"
    assert len(projection.entries) == 2
    assert all(entry.path != ".env" for entry in projection.entries)
    assert projection.truncated
