"""Authorization callbacks cannot leave the model's instruction context stale."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from stellarweaver.config import ContextConfig
from stellarweaver.context import ContextManager
from stellarweaver.domain import AssistantReply, JSONDict, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def reply(name: str, arguments: JSONDict, identifier: str) -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall(identifier, name, json.dumps(arguments)),), stop_reason="tool_calls"
    )


@pytest.mark.parametrize(
    "change", ["modify-root", "add-root", "delete-root", "modify-subdir", "add-subdir"]
)
async def test_instruction_changes_during_approval_are_reported_before_any_write(
    tmp_path: Path, change: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    if change != "add-root":
        (root / "AGENTS.md").write_text("INITIAL_ROOT: preserve user baseline.", encoding="utf-8")
    if change == "modify-subdir":
        (root / "src/AGENTS.md").write_text("INITIAL_SUBDIR", encoding="utf-8")
    target = root / "src/value.py"
    original = b"value = 2\n"
    target.write_bytes(original)
    unrelated = root / "user.txt"
    unrelated.write_bytes(b"user baseline\r\n")
    hook_calls: list[tuple[str, str]] = []
    approval_calls = 0

    async def approve(invocation: JSONDict) -> str:
        nonlocal approval_calls
        approval_calls += 1
        assert invocation["tool"] == "edit_file"
        if change == "delete-root":
            (root / "AGENTS.md").unlink()
        else:
            path = root / ("src/AGENTS.md" if change.endswith("subdir") else "AGENTS.md")
            path.write_text(
                "NEW_RULE: review the updated scope before editing value.py.", encoding="utf-8"
            )
        return "once"

    async def before(call: ToolCall, invocation: JSONDict) -> None:
        hook_calls.append(("before", call.call_id))

    async def after(call: ToolCall, invocation: JSONDict) -> None:
        hook_calls.append(("after", call.call_id))

    model = FakeModel(
        [
            reply("read_file", {"path": "src/value.py"}, "read"),
            reply(
                "edit_file",
                {
                    "path": "src/value.py",
                    "expected_sha256": hashlib.sha256(original).hexdigest(),
                    "old_text": "2",
                    "new_text": "3",
                },
                "edit",
            ),
            AssistantReply("Updated instructions require a new edit decision."),
        ]
    )
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(
            root,
            PolicyEngine(workspace=root, interactive=True, approval=approve),
            artifacts_dir=store.artifacts_dir,
        )
        registry.before_execute = before
        registry.after_authorize = after
        outcome = await Agent(model, registry, store=store).run("Change value to three.")
        assert outcome.run_status == "completed", outcome.reason
        results = store.load_results()
        assert len(results) == 2
        assert results[0].status == "ok"
        assert results[1].status == "error" and results[1].data["code"] == "INSTRUCTIONS_UPDATED"
        assert target.read_bytes() == original
        assert unrelated.read_bytes() == b"user baseline\r\n"
        assert approval_calls == 1
        assert hook_calls == [
            ("before", "read"),
            ("after", "read"),
            ("before", "edit"),
            ("after", "edit"),
        ]
        assert registry.before_execute is before and registry.after_authorize is after
        assert [
            event.payload["tool"] for event in store.load_events() if event.type == "tool.started"
        ] == ["read_file"]
        assert not list(store.artifacts_dir.iterdir())
        # Both the successful read and the refused edit remain complete protocol groups.
        ContextManager().build(
            store.load_messages(), (), 1, 100000, ContextConfig(reserve_tokens=0)
        )
        last_context = "\n".join(
            message.content for message in model.requests[-1].messages if message.role == "system"
        )
        if change == "delete-root":
            assert "INITIAL_ROOT" not in last_context
        else:
            assert "NEW_RULE" in last_context


@pytest.mark.parametrize("change_target", [False, True])
async def test_unchanged_instructions_preserve_permission_and_file_hash_recheck(
    tmp_path: Path, change_target: bool
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text("UNCHANGED_RULE: preserve unrelated files.", encoding="utf-8")
    target = root / "value.py"
    original = b"value = 2\n"
    target.write_bytes(original)
    approval_calls = 0
    after_calls = 0

    async def approve(invocation: JSONDict) -> str:
        nonlocal approval_calls
        approval_calls += 1
        if change_target:
            target.write_bytes(b"user changed during approval\n")
        return "once"

    async def after(call: ToolCall, invocation: JSONDict) -> None:
        nonlocal after_calls
        after_calls += 1

    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(
            root,
            PolicyEngine(workspace=root, interactive=True, approval=approve),
            artifacts_dir=store.artifacts_dir,
        )
        registry.after_authorize = after
        model = FakeModel(
            [
                reply(
                    "edit_file",
                    {
                        "path": "value.py",
                        "expected_sha256": hashlib.sha256(original).hexdigest(),
                        "old_text": "2",
                        "new_text": "3",
                    },
                    "edit",
                ),
                AssistantReply("Finished the requested attempt."),
            ]
        )
        outcome = await Agent(model, registry, store=store).run("Change value to three.")
        assert outcome.run_status == "completed"
        assert approval_calls == after_calls == 1
        result = store.load_results()[0]
        if change_target:
            assert result.status == "error" and result.data["code"] == "FILE_CHANGED"
            assert target.read_bytes() == b"user changed during approval\n"
            assert not any(event.type == "tool.started" for event in store.load_events())
        else:
            assert result.status == "ok"
            assert target.read_bytes() == b"value = 3\n"


async def test_stale_once_permission_is_not_reused_after_instruction_replanning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    instruction = root / "AGENTS.md"
    instruction.write_text("INITIAL_RULE", encoding="utf-8")
    target = root / "value.py"
    target.write_bytes(b"value = 2\n")
    approvals = 0

    async def approve(invocation: JSONDict) -> str:
        nonlocal approvals
        approvals += 1
        if approvals == 1:
            instruction.write_text("NEW_RULE: review before editing.", encoding="utf-8")
            return "once"
        return "deny"

    arguments = {
        "path": "value.py",
        "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "old_text": "2",
        "new_text": "3",
    }
    model = FakeModel(
        [reply("edit_file", arguments, "stale-edit"), reply("edit_file", arguments, "fresh-edit")]
    )
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(
            root,
            PolicyEngine(workspace=root, interactive=True, approval=approve),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(model, registry, store=store).run("Change the value.")
        assert outcome.run_status == "blocked"
        results = store.load_results()
        assert [result.status for result in results] == ["error", "denied"]
        assert results[0].data["code"] == "INSTRUCTIONS_UPDATED"
        assert approvals == 2
        assert target.read_bytes() == b"value = 2\n"
        assert not any(event.type == "tool.started" for event in store.load_events())
        ContextManager().build(
            store.load_messages(), (), 1, 100000, ContextConfig(reserve_tokens=0)
        )


@pytest.mark.parametrize("tool", ["create_file", "run_command"])
async def test_other_mutations_do_not_start_after_authorized_instruction_change(
    tmp_path: Path, tool: str
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    instruction = root / "AGENTS.md"
    instruction.write_text("INITIAL_RULE", encoding="utf-8")
    marker = root / "must-not-be-created.txt"
    if tool == "create_file":
        arguments: JSONDict = {"path": marker.name, "content": "stale instruction context"}
    else:
        arguments = {
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('must-not-be-created.txt').write_text('effect')",
            ]
        }

    async def approve(invocation: JSONDict) -> str:
        instruction.write_text(
            "UPDATED_RULE: inspect revised constraints before mutation.", encoding="utf-8"
        )
        return "once"

    model = FakeModel(
        [reply(tool, arguments, "mutation"), AssistantReply("Reconsider after the update.")]
    )
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(
            root,
            PolicyEngine(workspace=root, interactive=True, approval=approve),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(model, registry, store=store).run("Perform the requested mutation.")
        assert outcome.run_status == "completed"
        result = store.load_results()[0]
        assert result.status == "error" and result.data["code"] == "INSTRUCTIONS_UPDATED"
        assert not marker.exists()
        assert not any(event.type == "tool.started" for event in store.load_events())
        assert not list(store.artifacts_dir.iterdir())
