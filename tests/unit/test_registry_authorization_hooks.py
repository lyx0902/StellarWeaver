"""Registry hooks keep their invocation count and enforce the pre-action boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from stellarweaver.domain import JSONDict, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.workspace import ToolError


def edit() -> ToolCall:
    return ToolCall(
        "edit",
        "edit_file",
        json.dumps(
            {
                "path": "value.txt",
                "expected_sha256": hashlib.sha256(b"before").hexdigest(),
                "old_text": "before",
                "new_text": "after",
            }
        ),
    )


async def test_after_authorize_hook_error_is_structured_before_start(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before")
    stages: list[str] = []
    records: list[str] = []

    async def before(call: ToolCall, invocation: JSONDict) -> None:
        stages.append("before")

    async def after(call: ToolCall, invocation: JSONDict) -> None:
        stages.append("after")
        raise ToolError("INSTRUCTIONS_UPDATED", "Review the revised instructions.")

    registry = ToolRegistry(
        tmp_path,
        PolicyEngine("workspace-edit", workspace=tmp_path),
        journal=lambda kind, payload: records.append(kind),
    )
    registry.before_execute, registry.after_authorize = before, after
    result = await registry.dispatch(edit())
    assert result.status == "error" and result.data["code"] == "INSTRUCTIONS_UPDATED"
    assert target.read_bytes() == b"before"
    assert stages == ["before", "after"]
    assert records == ["tool.authorization", "tool.finished"]


async def test_denied_write_does_not_invoke_after_hook_and_reads_do_not_mutate(
    tmp_path: Path,
) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before")
    stages: list[tuple[str, str]] = []

    async def before(call: ToolCall, invocation: JSONDict) -> None:
        stages.append(("before", call.name))

    async def after(call: ToolCall, invocation: JSONDict) -> None:
        stages.append(("after", call.name))

    registry = ToolRegistry(tmp_path, PolicyEngine("read-only", workspace=tmp_path))
    registry.before_execute, registry.after_authorize = before, after
    denied = await registry.dispatch(edit())
    read = await registry.dispatch(ToolCall("read", "read_file", '{"path":"value.txt"}'))
    assert denied.status == "denied" and read.status == "ok"
    assert target.read_bytes() == b"before"
    assert stages == [("before", "edit_file"), ("before", "read_file"), ("after", "read_file")]
    assert not registry.artifacts_dir.exists()


async def test_original_file_recheck_still_follows_after_hook(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before")
    calls = 0

    async def after(call: ToolCall, invocation: JSONDict) -> None:
        nonlocal calls
        calls += 1
        target.write_bytes(b"observable external change")

    registry = ToolRegistry(tmp_path, PolicyEngine("workspace-edit", workspace=tmp_path))
    registry.after_authorize = after
    result = await registry.dispatch(edit())
    assert calls == 1
    assert result.status == "error" and result.data["code"] == "FILE_CHANGED"
    assert target.read_bytes() == b"observable external change"
