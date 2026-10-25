"""Applicable instruction discovery stops at the explicitly fixed workspace."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from stellarweaver.instructions import InstructionLoader, InstructionLoadError
from stellarweaver.tools.workspace import Workspace


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_root_and_deeper_scope_inherit_without_loading_sibling_or_external_parent(
    tmp_path: Path,
) -> None:
    write(tmp_path / "AGENTS.md", "external parent should never be loaded")
    root = tmp_path / "workspace"
    write(root / "AGENTS.md", "Root: keep public API")
    write(root / "src/AGENTS.md", "Python code uses typing")
    write(root / "src/中文/AGENTS.md", "中文模块约束")
    write(root / "other/AGENTS.md", "unrelated sibling")
    write(root / "src/中文/module.py", "value = 1")
    projection = InstructionLoader(root).load_for_path("src/中文/module.py")
    assert [source.source_path for source in projection.sources] == [
        "AGENTS.md",
        "src/AGENTS.md",
        "src/中文/AGENTS.md",
    ]
    assert [source.scope for source in projection.sources] == [".", "src", "src/中文"]
    assert projection.changed
    assert all(message.role == "system" for message in projection.messages)
    assert "not authorization" in projection.messages[0].content
    assert "explicit user instructions take precedence" in projection.messages[0].content
    assert "Parent constraints remain inherited" in projection.messages[0].content
    assert all("external parent" not in message.content for message in projection.messages)


def test_repeated_load_is_stable_and_changed_hash_is_reloaded(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    write(path, "first rule")
    loader = InstructionLoader(tmp_path)
    first = loader.load_root()
    second = loader.load_root()
    assert not second.changed
    assert second.messages == first.messages
    assert second.sources[0].loaded_at == first.sources[0].loaded_at
    write(path, "second rule")
    third = loader.load_root()
    assert third.changed
    assert third.sources[0].sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert third.sources[0].sha256 != first.sources[0].sha256
    assert third.sources[0].content == "second rule"
    path.unlink()
    assert loader.load_root().sources == ()


def test_new_file_target_loads_existing_parents(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "root")
    write(tmp_path / "src/AGENTS.md", "src")
    projection = InstructionLoader(tmp_path).load_for_path("src/new/nested/file.py")
    assert [source.source_path for source in projection.sources] == ["AGENTS.md", "src/AGENTS.md"]


def test_directory_target_and_multiple_scope_union_are_deterministic(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "root")
    write(tmp_path / "a/AGENTS.md", "scope a")
    write(tmp_path / "b/AGENTS.md", "scope b")
    loader = InstructionLoader(tmp_path)
    projection = loader.for_paths(["b/", "a/"])
    assert [source.source_path for source in projection.sources] == [
        "AGENTS.md",
        "a/AGENTS.md",
        "b/AGENTS.md",
    ]
    assert not loader.for_paths(["a/", "b/"]).changed


@pytest.mark.parametrize(
    "path", ["../outside/file.py", ".env", ".aws/config", "C:/outside/file.py"]
)
def test_outside_or_protected_target_is_visible_without_loading_its_scope(
    tmp_path: Path, path: str
) -> None:
    write(tmp_path / "AGENTS.md", "root")
    projection = InstructionLoader(tmp_path).load_for_path(path)
    assert len(projection.sources) == 1
    assert projection.warnings


def test_ignored_and_runtime_owned_directories_are_not_read(tmp_path: Path) -> None:
    write(tmp_path / ".gitignore", "ignored/\n")
    write(tmp_path / "AGENTS.md", "root")
    write(tmp_path / "ignored/AGENTS.md", "ignored rule")
    write(tmp_path / "state/AGENTS.md", "runtime owned rule")
    workspace = Workspace(tmp_path)
    workspace.protect(tmp_path / "state")
    loader = InstructionLoader(workspace)
    projection = loader.for_paths(["ignored/file.py", "state/file.py"])
    assert [source.source_path for source in projection.sources] == ["AGENTS.md"]
    assert len(projection.warnings) == 2


def test_byte_limit_fails_closed_instead_of_silently_skipping_instructions(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "中文" * 20)
    with pytest.raises(InstructionLoadError, match="byte limit"):
        InstructionLoader(tmp_path, max_file_bytes=32).load_root()


def test_total_limit_includes_every_applicable_file(tmp_path: Path) -> None:
    write(tmp_path / "AGENTS.md", "a" * 40)
    write(tmp_path / "src/AGENTS.md", "b" * 40)
    with pytest.raises(InstructionLoadError, match="total byte limit"):
        InstructionLoader(tmp_path, max_file_bytes=50, max_total_bytes=60).load_for_path("src/x.py")


@pytest.mark.parametrize("content", [b"\xff", b"rules\0binary"])
def test_invalid_instruction_content_is_reported(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "AGENTS.md").write_bytes(content)
    with pytest.raises(InstructionLoadError):
        InstructionLoader(tmp_path).load_root()


def test_instruction_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "external-rules.txt"
    write(target, "should not be loaded as instructions")
    try:
        (tmp_path / "AGENTS.md").symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this Windows account")
    projection = InstructionLoader(tmp_path).load_root()
    assert projection.sources == ()
    assert any("symlink" in warning for warning in projection.warnings)


def test_directory_symlink_does_not_import_other_scope(tmp_path: Path) -> None:
    write(tmp_path / "real/AGENTS.md", "real scope")
    try:
        (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:
        pytest.skip("Creating directory symlinks is unavailable for this Windows account")
    projection = InstructionLoader(tmp_path).load_for_path("alias/file.py")
    assert projection.sources == ()
    assert projection.warnings
