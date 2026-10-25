from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from stellarweaver.domain import ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.tools.registry import ToolRegistry


def call(name: str, **arguments: object) -> ToolCall:
    return ToolCall("call-test", name, json.dumps(arguments))


def registry(
    tmp_path: Path, preset: str = "workspace-edit", **policy_options: object
) -> ToolRegistry:
    return ToolRegistry(tmp_path, PolicyEngine(preset, workspace=tmp_path, **policy_options))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_real_read_edit_with_unicode_spaces_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "中文 file.py"
    path.write_text("value = 1\n", encoding="utf-8", newline="")
    tools = registry(tmp_path)
    read = await tools.dispatch(call("read_file", path=path.name))
    assert read.status == "ok"
    assert read.content == "1: value = 1"
    result = await tools.dispatch(
        call(
            "edit_file",
            path=path.name,
            old_text="value = 1",
            new_text="value = 2",
            expected_sha256=read.data["sha256"],
        )
    )
    assert result.status == "ok"
    assert path.read_text(encoding="utf-8") == "value = 2\n"
    artifact = Path(result.artifact_ids[0])
    assert await asyncio.to_thread(artifact.read_bytes) == b"value = 1\n"
    assert result.data["replacement_count"] == 1
    assert "+value = 2" in result.data["diff"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("old", "error"), [("x", "EDIT_AMBIGUOUS"), ("missing", "EDIT_NOT_FOUND")])
async def test_edit_requires_exactly_one_match(tmp_path: Path, old: str, error: str) -> None:
    target = tmp_path / "a.txt"
    original = b"x x\n"
    target.write_bytes(original)
    result = await registry(tmp_path).dispatch(
        call(
            "edit_file",
            path="a.txt",
            old_text=old,
            new_text="y",
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
    )
    assert result.status == "error"
    assert result.data["code"] == error
    assert target.read_bytes() == original


@pytest.mark.asyncio
async def test_stale_hash_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("new", encoding="utf-8")
    result = await registry(tmp_path).dispatch(
        call("edit_file", path="a.txt", old_text="new", new_text="bad", expected_sha256="0" * 64)
    )
    assert result.data["code"] == "FILE_CHANGED"
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_file_changed_during_approval_is_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_bytes(b"original")

    async def approval(invocation: dict[str, object]) -> str:
        assert "-original" in str(invocation["preview"])
        target.write_bytes(b"user change")
        return "once"

    tools = registry(tmp_path, "ask", interactive=True, approval=approval)
    result = await tools.dispatch(
        call(
            "edit_file",
            path="a.txt",
            old_text="original",
            new_text="agent change",
            expected_sha256=hashlib.sha256(b"original").hexdigest(),
        )
    )
    assert result.data["code"] == "FILE_CHANGED"
    assert target.read_bytes() == b"user change"


@pytest.mark.asyncio
async def test_crlf_and_file_mode_preserved(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    original = b"before\r\nnext\r\n"
    target.write_bytes(original)
    target.chmod(0o755)
    before_mode = target.stat().st_mode
    result = await registry(tmp_path).dispatch(
        call(
            "edit_file",
            path="a.txt",
            old_text="before",
            new_text="after\nadded",
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
    )
    assert result.status == "ok"
    assert target.read_bytes() == b"after\r\nadded\r\nnext\r\n"
    assert target.stat().st_mode == before_mode


@pytest.mark.asyncio
async def test_create_is_exclusive_and_creates_parent(tmp_path: Path) -> None:
    tools = registry(tmp_path)
    created = await tools.dispatch(call("create_file", path="new/path.txt", content="hello"))
    assert created.status == "ok"
    refused = await tools.dispatch(call("create_file", path="new/path.txt", content="overwrite"))
    assert refused.data["code"] == "FILE_EXISTS"
    assert (tmp_path / "new/path.txt").read_text() == "hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        ".git/config",
        ".env",
        ".env.local",
        ".stellarweaver/session.jsonl",
        "id_rsa",
        "private.key",
        "data.sqlite3",
    ],
)
async def test_path_boundaries_prevent_read_and_create(tmp_path: Path, path: str) -> None:
    result = await registry(tmp_path).dispatch(call("create_file", path=path, content="secret"))
    assert result.status == "error"
    assert result.data["code"] in {"PROTECTED_PATH", "PATH_OUTSIDE_WORKSPACE"}


@pytest.mark.asyncio
async def test_absolute_workspace_path_is_rejected(tmp_path: Path) -> None:
    result = await registry(tmp_path).dispatch(
        call("create_file", path=str(tmp_path / "a.txt"), content="bad")
    )
    assert result.data["code"] == "PATH_OUTSIDE_WORKSPACE"


@pytest.mark.asyncio
async def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    link = tmp_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation unsupported: {exc}")
    result = await registry(tmp_path).dispatch(
        call("create_file", path="escape/a.txt", content="bad")
    )
    assert result.data["code"] == "PATH_OUTSIDE_WORKSPACE"
    assert not (outside / "a.txt").exists()


@pytest.mark.asyncio
async def test_list_and_search_respect_ignores_and_protection(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (tmp_path / ".stellarweaverignore").write_text("extra.txt\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    for name in ("ignored/no.txt", "extra.txt", ".env", "visible.txt"):
        (tmp_path / name).write_text("-needle 你好\n", encoding="utf-8")
    tools = registry(tmp_path)
    listed = await tools.dispatch(call("list_files"))
    assert "visible.txt" in listed.data["paths"]
    assert not {"ignored/no.txt", "extra.txt", ".env"}.intersection(listed.data["paths"])
    result = await tools.dispatch(call("search_text", pattern="-needle", glob="*.txt"))
    assert result.status == "ok"
    assert [match["path"] for match in result.data["matches"]] == ["visible.txt"]


@pytest.mark.asyncio
async def test_read_is_bounded_with_complete_hash(tmp_path: Path) -> None:
    data = b"a\n" * 600
    (tmp_path / "a.txt").write_bytes(data)
    result = await registry(tmp_path).dispatch(call("read_file", path="a.txt"))
    assert len(result.data["lines"]) == 400
    assert result.data["total_lines"] == 600
    assert result.data["sha256"] == hashlib.sha256(data).hexdigest()
    assert result.data["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "code"), [(b"a\0b", "BINARY_FILE"), (b"\xff", "INVALID_ENCODING")]
)
async def test_read_returns_structured_content_errors(
    tmp_path: Path, data: bytes, code: str
) -> None:
    (tmp_path / "bad.txt").write_bytes(data)
    result = await registry(tmp_path).dispatch(call("read_file", path="bad.txt"))
    assert result.data["code"] == code


@pytest.mark.asyncio
async def test_journal_precedes_side_effect_and_result_is_correlated(tmp_path: Path) -> None:
    records: list[tuple[str, dict[str, object]]] = []

    async def journal(event: str, data: dict[str, object]) -> None:
        if event == "tool.started":
            assert not (tmp_path / "a.txt").exists()
        records.append((event, data))

    tools = registry(tmp_path)
    tools.journal = journal
    result = await tools.dispatch(call("create_file", path="a.txt", content="hello"))
    assert [event for event, _ in records] == [
        "tool.authorization",
        "tool.started",
        "tool.finished",
    ]
    assert all(data["operation_id"] == result.operation_id for _, data in records)
    assert result.call_id == "call-test"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_call", "code"),
    [
        (ToolCall("c", "read_file", "{"), "INVALID_JSON"),
        (call("read_file", path="a", typo=True), "INVALID_ARGUMENTS"),
        (call("read_file", path="a", start_line=True), "INVALID_ARGUMENTS"),
        (call("missing"), "UNKNOWN_TOOL"),
    ],
)
async def test_bad_calls_are_results_not_runtime_exceptions(
    tmp_path: Path, tool_call: ToolCall, code: str
) -> None:
    result = await registry(tmp_path).dispatch(tool_call)
    assert result.status == "error"
    assert result.data["code"] == code


@pytest.mark.asyncio
async def test_fallback_search_handles_dash_pattern_and_caps_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("stellarweaver.tools.search.shutil.which", lambda _: None)
    (tmp_path / "中文 file.txt").write_bytes(b"-option\n-option\n-option\n")
    result = await registry(tmp_path).dispatch(
        call("search_text", pattern="-option", max_matches=2)
    )
    assert result.status == "ok"
    assert result.data["backend"] == "python"
    assert result.data["count"] == 2
    assert result.data["truncated"] is True
    assert result.data["matches"][0]["line"] == 1


@pytest.mark.asyncio
async def test_very_large_utf8_line_is_bounded_with_exact_streamed_hash(tmp_path: Path) -> None:
    raw = ("你好" * 200000 + "\n").encode("utf-8")
    (tmp_path / "large.txt").write_bytes(raw)
    result = await registry(tmp_path).dispatch(call("read_file", path="large.txt"))
    assert result.status == "ok"
    assert result.data["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result.data["editable"] is False
    assert result.data["truncated"] is True
    assert len(result.content) < 20000


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["file.txt:stream", "CON", "LPT1.txt"])
async def test_windows_stream_and_device_paths_are_rejected(tmp_path: Path, path: str) -> None:
    result = await registry(tmp_path).dispatch(call("read_file", path=path))
    assert result.data["code"] == "INVALID_PATH"


@pytest.mark.asyncio
async def test_dynamic_session_root_is_protected_from_read_edit_and_enumeration(
    tmp_path: Path,
) -> None:
    state = tmp_path / "custom-state" / "this-session"
    state.mkdir(parents=True)
    (state / "before.txt").write_text("private backup evidence", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("private backup evidence", encoding="utf-8")
    tools = registry(tmp_path)
    tools.workspace.protect(state)
    read = await tools.dispatch(call("read_file", path="custom-state/this-session/before.txt"))
    assert read.data["code"] == "PROTECTED_PATH"
    create = await tools.dispatch(
        call("create_file", path="custom-state/this-session/new.txt", content="bad")
    )
    assert create.data["code"] == "PROTECTED_PATH"
    listed = await tools.dispatch(call("list_files"))
    assert listed.data["paths"] == ["visible.txt"]
    searched = await tools.dispatch(call("search_text", pattern="private"))
    assert searched.status == "ok"
    assert [match["path"] for match in searched.data["matches"]] == ["visible.txt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["session.sqlite3-wal", "session.sqlite3-shm", "state.sqlite-wal", "state.sqlite-shm"]
)
async def test_sqlite_sidecars_are_protected(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_bytes(b"private")
    result = await registry(tmp_path).dispatch(call("read_file", path=name))
    assert result.data["code"] == "PROTECTED_PATH"
