"""Registry hooks keep their invocation count and enforce the pre-action boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from stellarweaver.domain import AssistantReply, JSONDict, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.policy.hooks import Decision, HookEvent, HookManager, TrustedHook
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.web_search import register_search
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
    assert records == ["security.assessed", "tool.authorization", "tool.finished"]


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


@pytest.mark.parametrize("preset", ["read-only", "ask", "workspace-edit", "auto"])
async def test_public_search_is_allowed_without_approval(tmp_path: Path, preset) -> None:
    async def approve(_: JSONDict) -> str:
        pytest.fail("Public search must not request default approval")

    async def lookup(query: str) -> AssistantReply:
        assert query == "Python docs"
        return AssistantReply("Public result", web_sources=({"url": "https://docs.python.org/3/"},))

    records: list[tuple[str, JSONDict]] = []
    registry = ToolRegistry(
        tmp_path,
        PolicyEngine(preset, workspace=tmp_path, interactive=True, approval=approve),
        journal=lambda event, payload: records.append((event, payload)),
    )
    register_search(registry, lookup)
    result = await registry.dispatch(ToolCall("search", "ds_search", '{"query":"Python docs"}'))
    assert result.status == "ok"
    assert records[0][1]["level"] == "L0"
    assert records[1][0] == "tool.authorization"
    assert records[1][1]["decision"] == "allow"


@pytest.mark.parametrize("query", ["password=synthetic-marker", r"Z:\synthetic-marker\notes.txt"])
async def test_hard_egress_denial_precedes_hooks_and_never_reaches_receiver(
    tmp_path: Path, query: str
) -> None:
    def unexpected(*_args: object) -> None:
        pytest.fail("Hard guard must precede this callback")

    records: list[tuple[str, JSONDict]] = []
    registry = ToolRegistry(
        tmp_path,
        PolicyEngine("read-only", workspace=tmp_path, interactive=True, approval=unexpected),
        hooks=HookManager([TrustedHook("pre", "fixture", "1", "PreToolUse", unexpected)]),
        journal=lambda event, payload: records.append((event, payload)),
    )
    register_search(registry, unexpected)
    result = await registry.dispatch(ToolCall("search", "ds_search", json.dumps({"query": query})))
    assert result.status == "denied" and result.data["code"] == "NONPUBLIC_SEARCH_QUERY"
    assert records[0][0] == "security.assessed"
    assert records[0][1]["level"] == "L3"
    assert records[0][1]["capabilities"] == ["network:public-search-query"]
    assert "synthetic-marker" not in json.dumps(records)


async def test_trusted_hooks_are_ordered_and_can_require_one_shot_approval(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before")
    order: list[str] = []
    receipts: list[JSONDict] = []

    async def first(_event: HookEvent) -> Decision:
        order.append("first")
        return "ask"

    async def second(_event: HookEvent) -> Decision:
        order.append("second")
        return "continue"

    async def permission(_event: HookEvent) -> Decision:
        order.append("permission")
        assert target.read_bytes() == b"before"
        return "continue"

    async def approve(request: JSONDict) -> str:
        order.append("approve")
        assert request["one_shot"] is True
        assert request["security"]["capabilities"] == ["workspace:write"]
        return "once"

    async def post(event: HookEvent) -> Decision:
        order.append("post")
        assert target.read_bytes() == b"after"
        assert json.loads(event.result_json or "{}")["status"] == "ok"
        return "continue"

    hooks = HookManager(
        [
            TrustedHook("b", "fixture", "1", "PreToolUse", second),
            TrustedHook("a", "fixture", "1", "PreToolUse", first),
            TrustedHook("c", "fixture", "1", "PermissionRequest", permission),
            TrustedHook("d", "fixture", "1", "PostToolUse", post),
        ]
    )
    registry = ToolRegistry(
        tmp_path,
        PolicyEngine("workspace-edit", workspace=tmp_path, interactive=True, approval=approve),
        hooks=hooks,
        journal=lambda kind, payload: receipts.append(payload) if kind == "security.hook" else None,
    )
    result = await registry.dispatch(edit())
    assert result.status == "ok"
    assert order == ["first", "second", "permission", "approve", "post"]
    assert [record["identifier"] for record in receipts] == ["a", "b", "c", "d"]


@pytest.mark.parametrize("failure", ["deny", "exception", "timeout"])
async def test_security_hook_failures_prevent_edit(tmp_path: Path, failure: str) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before")

    async def hook(_: HookEvent) -> Decision:
        if failure == "exception":
            raise ValueError("private hook exception text")
        if failure == "timeout":
            await asyncio.sleep(10)
        return "deny"

    async def approve(_: JSONDict) -> str:
        pytest.fail("Approval must happen after the security hook")

    registry = ToolRegistry(
        tmp_path,
        PolicyEngine(workspace=tmp_path, interactive=True, approval=approve),
        hooks=HookManager(
            [TrustedHook("check", "fixture", "1", "PermissionRequest", hook, timeout_seconds=0.02)]
        ),
    )
    result = await registry.dispatch(edit())
    assert result.data["code"] == ("HOOK_DENIED" if failure == "deny" else "HOOK_FAILED_CLOSED")
    assert not registry.artifacts_dir.exists() and target.read_bytes() == b"before"
    assert "private hook exception text" not in result.content


async def test_authorization_is_rechecked_after_durable_intent(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before")

    def journal(kind: str, data: JSONDict) -> None:
        if kind == "tool.started":
            target.write_bytes(b"outside change")
            data["path"] = "unrelated.txt"

    async def approve(invocation: JSONDict) -> str:
        invocation["path"] = "unrelated.txt"
        invocation["security"]["capabilities"].clear()
        return "once"

    registry = ToolRegistry(
        tmp_path,
        PolicyEngine(workspace=tmp_path, interactive=True, approval=approve),
        journal=journal,
    )
    result = await registry.dispatch(edit())
    assert result.data["code"] == "FILE_CHANGED"
    assert target.read_bytes() == b"outside change"
    assert not registry.artifacts_dir.exists()


async def test_replacing_policy_during_search_approval_blocks_dispatch(tmp_path: Path) -> None:
    calls: list[str] = []

    async def require_approval(_: HookEvent) -> Decision:
        return "ask"

    async def approve(_invocation: JSONDict) -> str:
        registry.policy = PolicyEngine("read-only", workspace=tmp_path)
        return "once"

    async def lookup(query: str) -> AssistantReply:
        calls.append(query)
        return AssistantReply("No sources")

    registry = ToolRegistry(
        tmp_path,
        PolicyEngine("read-only", workspace=tmp_path, interactive=True, approval=approve),
        hooks=HookManager([TrustedHook("ask", "fixture", "1", "PreToolUse", require_approval)]),
    )
    register_search(registry, lookup)
    result = await registry.dispatch(ToolCall("search", "ds_search", '{"query":"Python docs"}'))
    assert result.data["code"] == "AUTHORIZATION_CHANGED"
    assert calls == []


async def test_post_observer_failure_preserves_result_and_withhold_is_explicit(
    tmp_path: Path,
) -> None:
    async def observer(_: HookEvent) -> Decision:
        raise RuntimeError("observer unavailable")

    async def redact(_: HookEvent) -> Decision:
        return "withhold"

    (tmp_path / "value.txt").write_bytes(b"before")
    registry = ToolRegistry(tmp_path, PolicyEngine("read-only", workspace=tmp_path))
    registry.hooks = HookManager([TrustedHook("observer", "fixture", "1", "PostToolUse", observer)])
    read = ToolCall("read", "read_file", '{"path":"value.txt"}')
    assert "before" in (await registry.dispatch(read)).content
    registry.hooks = HookManager([TrustedHook("redact", "fixture", "1", "PostToolUse", redact)])
    result = await registry.dispatch(read)
    assert result.status == "ok"
    assert result.data == {"code": "OUTPUT_WITHHELD"}
    assert "before" not in result.content
