"""Offline fixtures for patch recovery, ordered reads and scoped sandbox grants."""

import asyncio
import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from stellarweaver.changes import ChangesManager
from stellarweaver.config import AppConfig, HarnessConfig, LimitsConfig, PolicyConfig
from stellarweaver.context.manager import _project_tool
from stellarweaver.domain import AssistantReply, Message, ToolCall, ToolResult
from stellarweaver.policy import PolicyEngine
from stellarweaver.policy.hooks import HookManager, TrustedHook
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.development import decode_development
from stellarweaver.tools.workspace import sha256_bytes
from stellarweaver.web.isolation import git


def call(name, arguments, number=1):
    return ToolCall(str(number), name, json.dumps(arguments))


def calls(*items):
    return AssistantReply(tool_calls=items, stop_reason="tool_calls")


def repository(root: Path) -> Path:
    root.mkdir()
    (root / "modify.py").write_bytes(b"value = 1\n")
    (root / "delete.py").write_bytes(b"original = True\n")
    git(root, "init", "--template=")
    git(root, "add", "--", "modify.py", "delete.py")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=f@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return root


def patch():
    return call(
        "apply_patch",
        {
            "changes": [
                {
                    "path": "modify.py",
                    "expected_sha256": sha256_bytes(b"value = 1\n"),
                    "content": "value = (\n",
                },
                {"path": "create.py", "expected_sha256": None, "content": "def broken(:\n"},
                {
                    "path": "delete.py",
                    "expected_sha256": sha256_bytes(b"original = True\n"),
                    "content": None,
                },
            ],
        },
    )


def patch_config():
    return AppConfig(
        harness=HarnessConfig(multi_file_apply=True, post_edit_syntax_hook=True),
        policy=PolicyConfig(preset="workspace-edit"),
    )


def snapshot(root):
    return {
        path: (root / path).read_bytes() if (root / path).exists() else None
        for path in ("modify.py", "create.py", "delete.py")
    }


async def test_three_file_patch_and_grouped_cli_undo(tmp_path, monkeypatch):
    root = repository(tmp_path / "git")
    original = snapshot(root)
    index = git(root, "write-tree")
    apply_call = patch()
    original_replace = os.replace
    swaps = []
    with SessionStore(root, tmp_path / "state") as store:

        def replace_target(source, destination):
            if Path(destination).parent == root:
                if not swaps:
                    assert len(list(store.artifacts_dir.glob("*.before"))) == 2
                    intent = next(e for e in store.load_events() if e.type == "tool.started")
                    assert [entry["action"] for entry in intent.payload["files"]] == [
                        "modify",
                        "create",
                        "delete",
                    ]
                swaps.append(Path(destination).name)
            original_replace(source, destination)

        monkeypatch.setattr(os, "replace", replace_target)
        agent = build_agent(
            patch_config(), FakeModel([calls(apply_call), AssistantReply("Done")]), store
        )
        outcome = await agent.run("Apply the patch")
        assert outcome.run_status == "completed"
        result = agent.results[0]
        assert result.status == "ok" and result.data["workspace_applied"]
        assert swaps == ["modify.py", "create.py"]
        assert git(root, "write-tree") == index
        assert [item["path"] for item in result.data["syntax_diagnostics"]] == [
            "modify.py",
            "create.py",
        ]
        assert result.content.count("[syntax_diagnostic] line 1:") == 2
        manager = ChangesManager(root, store, agent.registry.policy)
        edited = snapshot(root)
        (root / "modify.py").write_bytes(b"user changed last undo target\n")
        conflicted = snapshot(root)
        assert (await manager.undo())["code"] == "UNDO_CONFLICT"
        assert snapshot(root) == conflicted
        (root / "modify.py").write_bytes(edited["modify.py"])
        restored = await manager.undo()
        assert restored["status"] == "ok" and restored["target_operation_id"] == result.operation_id
        assert snapshot(root) == original
        assert git(root, "write-tree") == index
        assert swaps[2:] == ["delete.py", "modify.py"]
        print("patch fixture: files=3 operations=1 backups=2 undo_operations=1 conflict_writes=0")


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    ("phase", "states", "status", "attention"),
    [
        ("before", ["before", "before", "before"], "cancelled", False),
        ("mixed", ["after", "before", "before"], "unknown", True),
        ("after", ["after", "after", "after"], "ok", False),
    ],
)
async def test_started_patch_recovery_from_disk(
    tmp_path, monkeypatch, phase, states, status, attention
):
    root = repository(tmp_path / "git")
    original_replace = os.replace
    request = patch()
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(patch_config(), FakeModel([]), store)
        assistant = store.record_assistant(Message("assistant", tool_calls=(request,)))
        operation = store.operation_id_for(assistant, request.call_id)

        def journal(kind, payload):
            if kind == "tool.authorization":
                store.record_approved(assistant, payload)
            if kind == "tool.started":
                store.record_intent(assistant, payload)
            if kind == "tool.finished":
                if phase == "after":
                    raise SimulatedCrash()
                pytest.fail("A crashed operation should have no final result")

        def crash_on_replace(source, destination):
            crash_name = {"before": "modify.py", "mixed": "create.py"}.get(phase)
            if crash_name and Path(destination) == root / crash_name:
                raise SimulatedCrash()
            original_replace(source, destination)

        agent.registry.journal = journal
        monkeypatch.setattr(os, "replace", crash_on_replace)
        with pytest.raises(SimulatedCrash):
            await agent.registry.dispatch(request, operation_id=operation)
        observed = snapshot(root)
        assert store.load_results() == ()
        report = store.reconcile()
        assert report["requires_attention"] is attention
        recovered = report["operations"][0]
        assert recovered["status"] == status
        assert [item["state"] for item in recovered["data"]["files_observed"]] == states
        assert snapshot(root) == observed
        assert store.reconcile()["operations"] == []
        if phase == "after":
            undo = await ChangesManager(root, store, agent.registry.policy).undo()
            assert undo["status"] == "ok" and snapshot(root)["create.py"] is None


async def test_patch_rolls_back_completed_swaps_after_normal_io_error(tmp_path, monkeypatch):
    root = repository(tmp_path / "git")
    original = snapshot(root)
    original_replace = os.replace

    def fail_creation(source, destination):
        if Path(destination) == root / "create.py":
            raise OSError("fixture I/O failure")
        original_replace(source, destination)

    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(patch_config(), FakeModel([]), store)
        monkeypatch.setattr(os, "replace", fail_creation)
        result = await agent.registry.dispatch(patch())
        assert result.status == "error" and result.data["code"] == "PATCH_WRITE_ROLLED_BACK"
        assert snapshot(root) == original


async def test_read_batch_finishes_in_original_order_and_runs_in_parallel(tmp_path):
    root = repository(tmp_path / "git")
    group = (
        call("list_files", {"path": "."}, 1),
        call("read_file", {"path": "modify.py"}, 2),
        call("search_text", {"path": "modify.py", "pattern": "value"}, 3),
        call("git_status", {}, 4),
    )
    by_name = {item.name: item.call_id for item in group}
    metrics = {}
    ordering = {}
    for parallel in (False, True):
        active = 0
        peak = 0
        began = []
        finished = []
        hooks = []

        async def post(event, *, seen=hooks, in_parallel=parallel, completed=finished):
            seen.append(event.tool)
            if in_parallel:
                assert len(completed) == 4
            return "continue"

        with SessionStore(root, tmp_path / str(parallel)) as store:
            config = AppConfig(harness=HarnessConfig(parallel_read_tools=parallel))
            agent = build_agent(
                config,
                FakeModel([calls(*group), AssistantReply("Done")]),
                store,
                hooks=HookManager([TrustedHook("fixture", "test", "1", "PostToolUse", post)]),
            )

            def add_delay(prepare, delay, *, starts=began, ends=finished):
                async def wrapped(args, context):
                    prepared = await prepare(args, context)

                    async def execute():
                        nonlocal active, peak
                        active += 1
                        peak = max(active, peak)
                        starts.append(time.perf_counter())
                        try:
                            await asyncio.sleep(delay)
                            return await prepared.execute()
                        finally:
                            active -= 1
                            ends.append(time.perf_counter())

                    return replace(prepared, execute=execute)

                return wrapped

            for number, item in enumerate(group):
                registered = agent.registry._tools[item.name]
                agent.registry._tools[item.name] = replace(
                    registered, prepare=add_delay(registered.prepare, 0.32 - number * 0.045)
                )
            assert (await agent.run("Read the workspace")).run_status == "completed"
            assert [result.status for result in store.load_results()] == ["ok"] * 4
            assert [
                message.tool_call_id for message in store.load_messages() if message.role == "tool"
            ] == list(by_name.values())
            assert hooks == list(by_name)
            events = store.load_events()
            assert [
                event.payload["outcome"] for event in events if event.type == "security.hook"
            ] == ["continue"] * 4
            trace = {}
            for kind in ("tool.authorization", "tool.started", "tool.completed", "tool.finished"):
                matches = [event for event in events if event.type == kind]
                assert [event.seq for event in matches] == sorted(event.seq for event in matches)
                trace[kind] = [event.payload["call_id"] for event in matches]
                assert trace[kind] == list(by_name.values())
            if parallel:
                assert max(e.seq for e in events if e.type == "tool.started") < min(
                    e.seq for e in events if e.type == "tool.completed"
                )
            ordering[parallel] = trace
            metrics[parallel] = (peak, round(max(finished) - min(began), 3))
    assert ordering[False] == ordering[True]
    assert metrics[False][0] == 1 and metrics[True][0] == 4
    assert metrics[True][1] < metrics[False][1]
    print(f"read fixture (peak active, elapsed seconds): {metrics}; journal order 4/4 per event")


async def test_interactive_read_flushes_prefix_and_leaves_later_read_serial(tmp_path):
    root = repository(tmp_path / "git")
    group = (
        call("list_files", {"path": "."}, 1),
        call("read_file", {"path": "modify.py"}, 2),
        call("git_status", {}, 3),
    )
    entered = []
    with SessionStore(root, tmp_path / "state") as store:

        async def pre(event):
            return "ask" if event.tool == "read_file" else "continue"

        async def approval(invocation):
            entered.extend(result.call_id for result in store.load_results())
            return "once"

        agent = build_agent(
            AppConfig(harness=HarnessConfig(parallel_read_tools=True)),
            FakeModel([calls(*group), AssistantReply("Done")]),
            store,
            interactive=True,
            approval=approval,
            hooks=HookManager([TrustedHook("fixture", "test", "1", "PreToolUse", pre)]),
        )
        assert (await agent.run("Read the workspace")).run_status == "completed"
        assert entered == ["1"]
        assert [result.call_id for result in store.load_results()] == ["1", "2", "3"]


async def test_read_group_preserves_budget_boundary(tmp_path):
    root = repository(tmp_path / "git")
    group = tuple(call("read_file", {"path": "modify.py"}, n) for n in range(3))
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(
            AppConfig(
                harness=HarnessConfig(parallel_read_tools=True),
                limits=LimitsConfig(max_tool_calls=2),
            ),
            FakeModel([calls(*group)]),
            store,
        )
        assert (await agent.run("Read within budget")).run_status == "budget_exceeded"
        assert [result.status for result in store.load_results()] == ["ok", "ok", "blocked"]


async def test_scoped_test_grant_reuses_target_only_and_restores_from_journal(tmp_path):
    root = repository(tmp_path / "git")
    targets = ("modify.py", "delete.py")
    requests = [
        call("run_tests", {"target": target, "framework": "pytest"}, i)
        for i, target in enumerate(targets, 1)
    ]
    approvals = []
    executed = []

    async def runner(argv, cwd, **kwargs):
        action, args = decode_development(argv)
        assert action == "run_tests" and cwd == root
        executed.append(args.target)
        return {"status": "ok", "data": {"exit_code": 0}}

    async def approval(invocation):
        approvals.append(invocation)
        return "session"

    measurement = {}
    for enabled in (False, True):
        approvals.clear()
        executed.clear()
        with SessionStore(root, tmp_path / str(enabled)) as store:
            config = AppConfig(
                harness=HarnessConfig(development_tools=True, scoped_test_grants=enabled)
            )
            agent = build_agent(
                config,
                FakeModel([calls(*requests), AssistantReply("Done")]),
                store,
                interactive=True,
                approval=approval,
                command_runner=runner,
                command_boundary="A",
            )
            assert (await agent.run("Test two files")).run_status == "completed"
            assert executed == list(targets)
            assert len(approvals) == (1 if enabled else 2)
            measurement[enabled] = len(approvals)
            if enabled:
                assert approvals[0]["offer_test_scope"] and not approvals[0]["one_shot"]
                saved = store.prior_test_grants()
                assert len(saved) == 1
                policy = PolicyEngine(
                    "ask", workspace=root, interactive=True, scoped_test_grants=True
                )
                policy.restore_test_grants(saved)
                call_data = {**saved[0], "tool": "run_command"}
                assert (await policy.decide(call_data))["decision"] == "allow"
                for wrong in (
                    {"command_boundary": "B"},
                    {"executor": "host"},
                    {"registered_tool": "run_command"},
                    {"argv": ["python", "-m", "pytest", "delete.py", "--unexpected-option"]},
                    {"workspace": str(root.parent)},
                    {"framework": "unittest"},
                ):
                    assert (await policy.decide({**call_data, **wrong}))["decision"] != "allow"
                assert (await policy.decide(call_data, force_approval=True))[
                    "decision"
                ] == "blocked"
    print(f"sandbox approvals for two files: {measurement}")


async def test_policy_file_grant_excludes_host_and_other_framework(tmp_path):
    root = repository(tmp_path / "git")
    policy_file = tmp_path / "grants.json"
    policy_file.write_text(
        json.dumps(
            {
                "workspace": str(root),
                "grants": [
                    {"tool": "run_tests", "command_boundary": "fixture", "framework": "pytest"},
                ],
            }
        )
    )

    async def runner(argv, cwd, **kwargs):
        return {"status": "ok", "data": {"exit_code": 0}}

    with SessionStore(root, tmp_path / "state") as store:
        config = AppConfig(harness=HarnessConfig(development_tools=True, scoped_test_grants=True))
        agent = build_agent(
            config,
            FakeModel([]),
            store,
            policy_file=policy_file,
            command_runner=runner,
            command_boundary="fixture",
        )
        for path in ("modify.py", "delete.py"):
            assert (
                await agent.registry.dispatch(
                    call("run_tests", {"target": path, "framework": "pytest"})
                )
            ).status == "ok"
        denied = await agent.registry.dispatch(call("run_tests", {"target": "modify.py"}))
        assert denied.status == "blocked"


@pytest.mark.parametrize("smart", [False, True])
def test_syntax_hint_survives_long_diff_projection(smart):
    diagnostic = {"line": 9, "message": "invalid syntax"}
    result = ToolResult(
        "1",
        "file",
        "edit_file",
        "ok",
        "large diff\n" + "变更行\n" * 800 + "[syntax_diagnostic] line 9: invalid syntax",
        {"syntax_diagnostic": diagnostic},
    )
    message = Message("tool", json.dumps(asdict(result), ensure_ascii=False), tool_call_id="1")
    output = json.loads(_project_tool(message, 384, smart=smart).content)
    assert "[syntax_diagnostic] line 9: invalid syntax" in output["content"]
    assert output["data"]["syntax_diagnostic"] == diagnostic
