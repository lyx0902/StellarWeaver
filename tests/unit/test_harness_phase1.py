"""Unit tests for Harness Phase 1 (Tasks 1.1 - 1.4)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from stellarweaver.config import AppConfig, HarnessConfig
from stellarweaver.domain import (
    AssistantReply,
    ModelCapabilities,
    ModelRequest,
    ToolCall,
    ToolResult,
)
from stellarweaver.evals.swebench import (
    Recipe,
    RunConfiguration,
    Task,
    run_config_sha256,
    save,
    solve_task,
)
from stellarweaver.evals.swebench_pilot import register_fixed_tools
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import build_agent
from stellarweaver.runtime.task_evidence import TaskEvidence
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import execute_command
from stellarweaver.tools.development import register_development_tools
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.staging import register_apply_patch, register_staging
from stellarweaver.tools.workspace import sha256_bytes


def _init_git_repo(root: Path) -> str:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "base"], check=True)
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()


# ---------------------------------------------------------------------------
# Task 1.1: LF / CRLF compatibility for edit_file, MultiEditFileArgs, staging
# ---------------------------------------------------------------------------


async def test_edit_file_pure_lf_and_pure_crlf(tmp_path: Path) -> None:
    lf_file = tmp_path / "lf.py"
    lf_file.write_bytes(b"a = 1\nb = 2\nc = 3\n")
    crlf_file = tmp_path / "crlf.py"
    crlf_file.write_bytes(b"x = 1\r\ny = 2\r\nz = 3\r\n")

    engine = PolicyEngine("workspace-edit", workspace=tmp_path)
    registry = ToolRegistry(tmp_path, engine)

    res_lf = await registry.dispatch(
        ToolCall(
            "c1",
            "edit_file",
            json.dumps(
                {
                    "path": "lf.py",
                    "old_text": "a = 1\nb = 2\n",
                    "new_text": "a = 10\nb = 20\n",
                    "expected_sha256": sha256_bytes(lf_file.read_bytes()),
                }
            ),
        )
    )
    assert res_lf.status == "ok"
    assert lf_file.read_bytes() == b"a = 10\nb = 20\nc = 3\n"

    # Model passes LF old_text & LF new_text against a pure CRLF file.
    res_crlf = await registry.dispatch(
        ToolCall(
            "c2",
            "edit_file",
            json.dumps(
                {
                    "path": "crlf.py",
                    "old_text": "x = 1\ny = 2\n",
                    "new_text": "x = 10\ny = 20\n",
                    "expected_sha256": sha256_bytes(crlf_file.read_bytes()),
                }
            ),
        )
    )
    assert res_crlf.status == "ok", res_crlf
    assert crlf_file.read_bytes() == b"x = 10\r\ny = 20\r\nz = 3\r\n"


async def test_edit_file_mixed_line_endings_preserves_surrounding_bytes(tmp_path: Path) -> None:
    mixed = tmp_path / "mixed.py"
    mixed.write_bytes(b"head = 0\r\na = 1\r\nb = 2\nc = 3\r\ntail = 4\n")
    engine = PolicyEngine("workspace-edit", workspace=tmp_path)
    registry = ToolRegistry(tmp_path, engine)

    res = await registry.dispatch(
        ToolCall(
            "c1",
            "edit_file",
            json.dumps(
                {
                    "path": "mixed.py",
                    "old_text": "a = 1\nb = 2\nc = 3",
                    "new_text": "a = 11\nb = 22\nc = 33",
                    "expected_sha256": sha256_bytes(mixed.read_bytes()),
                }
            ),
        )
    )
    assert res.status == "ok", res
    raw = mixed.read_bytes()
    assert raw.startswith(b"head = 0\r\n")
    assert raw.endswith(b"\r\ntail = 4\n")
    assert b"a = 11\r\nb = 22\r\nc = 33" in raw


async def test_multi_edit_file_crlf_ambiguity_and_overlap(tmp_path: Path) -> None:
    target = tmp_path / "multi.py"
    target.write_bytes(b"dup = 1\r\nmid = 2\r\ndup = 1\r\nend = 3\r\n")
    engine = PolicyEngine("workspace-edit", workspace=tmp_path)
    registry = ToolRegistry(tmp_path, engine, harness=HarnessConfig(multi_edit_tools=True))

    # Ambiguous CRLF match must fail with EDIT_AMBIGUOUS.
    amb = await registry.dispatch(
        ToolCall(
            "c1",
            "edit_file",
            json.dumps(
                {
                    "path": "multi.py",
                    "edits": [{"old_text": "dup = 1\n", "new_text": "dup = 9\n"}],
                    "expected_sha256": sha256_bytes(target.read_bytes()),
                }
            ),
        )
    )
    assert amb.status == "error"
    assert amb.data["code"] == "EDIT_AMBIGUOUS"

    # Overlapping spans must fail with EDIT_OVERLAPPING.
    ovl = await registry.dispatch(
        ToolCall(
            "c2",
            "edit_file",
            json.dumps(
                {
                    "path": "multi.py",
                    "edits": [
                        {"old_text": "dup = 1\nmid = 2\n", "new_text": "x\n"},
                        {"old_text": "mid = 2\ndup = 1\n", "new_text": "y\n"},
                    ],
                    "expected_sha256": sha256_bytes(target.read_bytes()),
                }
            ),
        )
    )
    assert ovl.status == "error"
    assert ovl.data["code"] == "EDIT_OVERLAPPING"

    # Two distinct CRLF spans edited cleanly in one call.
    ok = await registry.dispatch(
        ToolCall(
            "c3",
            "edit_file",
            json.dumps(
                {
                    "path": "multi.py",
                    "edits": [
                        {"old_text": "dup = 1\nmid = 2\n", "new_text": "first = 10\nmid = 20\n"},
                        {"old_text": "end = 3\n", "new_text": "end = 30\n"},
                    ],
                    "expected_sha256": sha256_bytes(target.read_bytes()),
                }
            ),
        )
    )
    assert ok.status == "ok", ok
    assert target.read_bytes() == b"first = 10\r\nmid = 20\r\ndup = 1\r\nend = 30\r\n"


async def test_stage_and_apply_patch_preserve_crlf_and_hashes(tmp_path: Path) -> None:
    target = tmp_path / "mod.py"
    original = b"line1 = 1\r\nline2 = 2\r\nline3 = 3\r\n"
    target.write_bytes(original)
    _init_git_repo(tmp_path)

    engine = PolicyEngine(
        "benchmark", workspace=tmp_path, allow_benchmark_staging=True
    )
    registry = ToolRegistry(tmp_path, engine)
    register_staging(registry)
    register_apply_patch(registry)

    payload = {
        "changes": [
            {
                "path": "mod.py",
                "expected_sha256": sha256_bytes(original),
                "content": "line1 = 1\nline2 = 200\nline3 = 3\n",
            }
        ]
    }
    staged = await registry.dispatch(ToolCall("s1", "stage_patch", json.dumps(payload)))
    assert staged.status == "ok", staged
    # Diff must only touch line2, not flip line1/line3 from CRLF to LF.
    assert "-line1 = 1" not in staged.data["patch"]
    assert "-line2 = 2" in staged.data["patch"]

    applied = await registry.dispatch(ToolCall("a1", "apply_patch", json.dumps(payload)))
    assert applied.status == "ok", applied
    expected_bytes = b"line1 = 1\r\nline2 = 200\r\nline3 = 3\r\n"
    assert target.read_bytes() == expected_bytes
    assert applied.data["files"][0]["after_sha256"] == sha256_bytes(expected_bytes)
    assert applied.data["files"][0]["after_sha256"] == staged.data["changes"]["mod.py"]["after_sha256"]


# ---------------------------------------------------------------------------
# Task 1.2: run_linter graceful skip and include_linter switch
# ---------------------------------------------------------------------------


def test_include_linter_false_hides_run_linter_schema(tmp_path: Path) -> None:
    async def runner(argv, cwd, *, config=None, artifacts_dir=None, timeout_seconds=None):
        return {"status": "ok", "content": "", "data": {"exit_code": 0}}

    engine = PolicyEngine("auto", workspace=tmp_path, allow_sandbox_development=True)
    reg_no_lint = ToolRegistry(tmp_path, engine, command_runner=runner, command_boundary="b")
    register_development_tools(reg_no_lint, include_linter=False)
    names_no_lint = {schema["function"]["name"] for schema in reg_no_lint.schemas()}
    assert "run_linter" not in names_no_lint
    assert {"run_tests", "discover_tests", "check_syntax", "run_python_snippet"} <= names_no_lint

    config = AppConfig(
        policy={"preset": "benchmark"},
        harness=HarnessConfig(development_tools=True, include_linter=False),
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = build_agent(
            config,
            FakeModel([]),
            store,
            command_runner=runner,
            command_boundary="bench",
            allow_sandbox_development=True,
        )
        names = {schema["function"]["name"] for schema in agent.registry.schemas()}
    assert "run_linter" not in names


async def test_run_linter_skips_gracefully_without_ruff(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")

    async def no_ruff_runner(argv, cwd, *, config=None, artifacts_dir=None, timeout_seconds=None):
        # Block ruff discovery inside the child interpreter without mutating host state.
        assert argv[:4] == ["python", "-I", "-B", "-c"]
        worker_code = (
            "import importlib.util as _u; _orig = _u.find_spec; "
            "_u.find_spec = lambda name, *a, **kw: None if name == 'ruff' else _orig(name, *a, **kw)\n"
            + argv[4]
        )
        patched = [sys.executable, "-I", "-B", "-c", worker_code, *argv[5:]]
        return await execute_command(
            patched, cwd, config=config, artifacts_dir=artifacts_dir, timeout_seconds=timeout_seconds
        )

    engine = PolicyEngine("auto", workspace=tmp_path, allow_sandbox_development=True)
    registry = ToolRegistry(
        tmp_path, engine, command_runner=no_ruff_runner, command_boundary="no-ruff"
    )
    register_development_tools(registry, include_linter=True)

    res = await registry.dispatch(
        ToolCall("c1", "run_linter", json.dumps({"target": "sample.py"}))
    )
    assert res.status == "ok", res
    assert res.data["exit_code"] == 0
    assert "Traceback" not in res.content
    assert res.data["diagnostic"]["skipped"] is True
    assert res.data["diagnostic"]["reason"] == "ruff_not_installed"


# ---------------------------------------------------------------------------
# Task 1.3: TaskEvidence apply_patch tracking & soft test failure reminder
# ---------------------------------------------------------------------------


def test_task_evidence_tracks_apply_patch_and_resets_unresolved_failures() -> None:
    ev = TaskEvidence()
    ev.observe(
        ToolResult(
            call_id="c1",
            operation_id="op1",
            tool_name="run_visible_tests",
            status="error",
            content="failed",
            data={"exit_code": 1},
        ),
        seq=1,
    )
    assert len(ev.unresolved_failures) == 1

    h1 = "a" * 64
    h2 = "b" * 64
    ev.observe(
        ToolResult(
            call_id="c2",
            operation_id="op2",
            tool_name="apply_patch",
            status="ok",
            content="Applied 3 file(s).",
            data={
                "files": [
                    {"path": "new.py", "action": "create", "before_sha256": None, "after_sha256": h1},
                    {"path": "mod.py", "action": "modify", "before_sha256": h1, "after_sha256": h2},
                    {"path": "old.py", "action": "delete", "before_sha256": h1, "after_sha256": None},
                ]
            },
        ),
        seq=2,
    )
    assert ev.edits["new.py"] == h1
    assert ev.edits["mod.py"] == h2
    assert ev.edits["old.py"] == "deleted"
    assert "old.py" not in ev.paths
    assert ev.last_check is None
    assert ev.unresolved_failures == {}


async def test_soft_reminder_triggers_after_visible_fail_and_local_pass(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_bytes(b"val = 1\n")
    visible_calls = 0

    async def runner(argv, cwd, *, config=None, artifacts_dir=None, timeout_seconds=None):
        nonlocal visible_calls
        if argv == ["pytest", "-q"]:
            visible_calls += 1
            if visible_calls == 1:
                return {"status": "error", "content": "1 failed", "data": {"exit_code": 1}}
            return {"status": "ok", "content": "1 passed", "data": {"exit_code": 0}}
        return {"status": "ok", "content": "local ok", "data": {"exit_code": 0}}

    config = AppConfig(
        policy={"preset": "benchmark"},
        harness=HarnessConfig(
            development_tools=True,
            include_linter=False,
            soft_test_failure_reminder=True,
        ),
    )
    sha1 = sha256_bytes(target.read_bytes())
    sha2 = sha256_bytes(b"val = 2\n")
    model = FakeModel(
        [
            AssistantReply(
                "",
                (
                    ToolCall(
                        "c1",
                        "edit_file",
                        json.dumps(
                            {
                                "path": "app.py",
                                "old_text": "val = 1\n",
                                "new_text": "val = 2\n",
                                "expected_sha256": sha1,
                            }
                        ),
                    ),
                ),
                "tool_calls",
            ),
            # run_visible_tests fails, then local run_tests passes.
            AssistantReply("", (ToolCall("c2", "run_visible_tests", "{}"),), "tool_calls"),
            AssistantReply(
                "",
                (
                    ToolCall(
                        "c3",
                        "run_tests",
                        json.dumps({"target": "app.py", "framework": "pytest"}),
                    ),
                ),
                "tool_calls",
            ),
            # Premature finish -> must be intercepted once by soft reminder.
            AssistantReply("Done early."),
            # After reminder, model fixes and reruns run_visible_tests (now passes).
            AssistantReply(
                "",
                (
                    ToolCall(
                        "c4",
                        "edit_file",
                        json.dumps(
                            {
                                "path": "app.py",
                                "old_text": "val = 2\n",
                                "new_text": "val = 3\n",
                                "expected_sha256": sha2,
                            }
                        ),
                    ),
                ),
                "tool_calls",
            ),
            AssistantReply("", (ToolCall("c5", "run_visible_tests", "{}"),), "tool_calls"),
            AssistantReply("Fixed and verified."),
        ]
    )
    policy_file = tmp_path / "policy.json"
    save(
        policy_file,
        {
            "workspace": str(tmp_path),
            "grants": [{"tool": "run_command", "argv": ["pytest", "-q"], "cwd": "."}],
        },
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = build_agent(
            config,
            model,
            store,
            policy_file=policy_file,
            command_runner=runner,
            command_boundary="b",
            allow_sandbox_development=True,
            sandbox_commands=(("pytest", "-q"),),
            stream=False,
        )
        register_fixed_tools(
            agent.registry, runner, {"run_visible_tests": ("visible", ("pytest", "-q"))}
        )
        outcome = await agent.run("Fix app.py")
        reminders = [ev for ev in store.load_events() if ev.type == "convergence.reminder"]

    assert outcome.run_status == "completed"
    assert outcome.final_text == "Fixed and verified."
    assert len(reminders) == 1
    assert visible_calls == 2


async def test_soft_reminder_fires_at_most_once_and_skips_pre_edit_repro(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("val = 1\n", encoding="utf-8")

    async def always_fail(argv, cwd, *, config=None, artifacts_dir=None, timeout_seconds=None):
        return {"status": "error", "content": "fail", "data": {"exit_code": 1}}

    config = AppConfig(
        policy={"preset": "benchmark"},
        harness=HarnessConfig(soft_test_failure_reminder=True),
    )
    # Scenario A: model ignores reminder -> must stop after at most 1 reminder.
    model_once = FakeModel(
        [
            AssistantReply("", (ToolCall("c1", "run_visible_tests", "{}"),), "tool_calls"),
            AssistantReply("First stop."),
            AssistantReply("Second stop after reminder."),
        ]
    )
    policy_file = tmp_path / "policy.json"
    save(
        policy_file,
        {
            "workspace": str(tmp_path),
            "grants": [{"tool": "run_command", "argv": ["pytest", "-q"], "cwd": "."}],
        },
    )
    with SessionStore(tmp_path, tmp_path / "state1") as store1:
        agent1 = build_agent(config, model_once, store1, policy_file=policy_file, stream=False)
        register_fixed_tools(
            agent1.registry, always_fail, {"run_visible_tests": ("visible", ("pytest", "-q"))}
        )
        out1 = await agent1.run("Test cap")
        rem1 = [ev for ev in store1.load_events() if ev.type == "convergence.reminder"]
    assert out1.run_status == "completed"
    assert out1.final_text == "Second stop after reminder."
    assert len(rem1) == 1

    # Scenario B: pre-edit repro fails -> edit -> rerun passes -> 0 reminders.
    calls = 0

    async def fail_then_pass(argv, cwd, *, config=None, artifacts_dir=None, timeout_seconds=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"status": "error", "content": "repro fail", "data": {"exit_code": 1}}
        return {"status": "ok", "content": "passed", "data": {"exit_code": 0}}

    sha1 = sha256_bytes(target.read_bytes())
    model_repro = FakeModel(
        [
            AssistantReply("", (ToolCall("r1", "run_visible_tests", "{}"),), "tool_calls"),
            AssistantReply(
                "",
                (
                    ToolCall(
                        "r2",
                        "edit_file",
                        json.dumps(
                            {
                                "path": "app.py",
                                "old_text": "val = 1\n",
                                "new_text": "val = 2\n",
                                "expected_sha256": sha1,
                            }
                        ),
                    ),
                ),
                "tool_calls",
            ),
            AssistantReply("", (ToolCall("r3", "run_visible_tests", "{}"),), "tool_calls"),
            AssistantReply("All green."),
        ]
    )
    with SessionStore(tmp_path, tmp_path / "state2") as store2:
        agent2 = build_agent(config, model_repro, store2, policy_file=policy_file, stream=False)
        register_fixed_tools(
            agent2.registry, fail_then_pass, {"run_visible_tests": ("visible", ("pytest", "-q"))}
        )
        out2 = await agent2.run("Fix after repro")
        rem2 = [ev for ev in store2.load_events() if ev.type == "convergence.reminder"]
    assert out2.run_status == "completed"
    assert rem2 == []


# ---------------------------------------------------------------------------
# Task 1.4: swebench.py cache aggregation ignores model.failed retries
# ---------------------------------------------------------------------------


class _FlakyCacheProvider:
    def __init__(self, *, omit_cache: bool = False) -> None:
        self.calls = 0
        self.omit_cache = omit_cache

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(
            tool_calling=True,
            streaming=False,
            parallel_tool_calls=False,
            context_window_tokens=200_000,
        )

    async def complete(self, request: ModelRequest) -> AssistantReply:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("transient connection reset", retryable=True)
        usage: dict[str, Any] = {"prompt_tokens": 1200, "completion_tokens": 40}
        if not self.omit_cache:
            usage["prompt_cache_hit_tokens"] = 960
        return AssistantReply(text="Done.", stop_reason="stop", usage=usage)

    async def stream(self, request: ModelRequest):  # pragma: no cover
        raise AssertionError("non-streaming test provider")
        yield  # type: ignore[misc]


@pytest.mark.parametrize("omit_cache", [False, True])
async def test_swebench_cache_aggregation_ignores_failed_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, omit_cache: bool
) -> None:
    assets = tmp_path / "assets"
    repo_dir = assets / "demo__pkg-1"
    repo_dir.mkdir(parents=True)
    (repo_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    base_commit = _init_git_repo(repo_dir)

    case_dir = tmp_path / "exp" / "cases" / "demo__pkg-1"
    attempt_dir = case_dir / "agent-1"
    attempt_dir.mkdir(parents=True)
    task = Task(
        instance_id="demo__pkg-1",
        repo="demo/pkg",
        base_commit=base_commit,
        problem_statement="Fix bug",
    )
    recipe = Recipe(image="sha256:" + "0" * 64, pytest_args=["-q"])
    save(case_dir / "task.json", task.model_dump())
    save(case_dir / "recipe.json", recipe.model_dump())

    import stellarweaver.evals.swebench as sw_mod

    monkeypatch.setattr(sw_mod, "local_image", lambda _img: None)
    monkeypatch.setattr(
        sw_mod, "build_provider", lambda *_a, **_kw: _FlakyCacheProvider(omit_cache=omit_cache)
    )

    ns = sw_mod.parser().parse_args(
        [
            "run",
            "--output",
            str(tmp_path / "exp"),
            "--assets",
            str(assets),
            "--live",
            "--no-sandbox-commands",
            "--no-development-tools",
        ]
    )
    manifest = {
        "context_window_tokens": 200_000,
        "limits": {
            "max_model_calls": None,
            "max_tool_calls": 20,
            "max_wall_seconds": 120,
            "max_input_tokens_total": 500_000,
            "max_output_tokens_total": 64_000,
        },
    }
    run_cfg = RunConfiguration.from_manifest(ns, manifest)
    await solve_task(case_dir, attempt_dir, ns, run_cfg, run_config_sha256(run_cfg.settings()))

    report = json.loads((attempt_dir / "agent-report.json").read_text(encoding="utf-8"))
    assert report["failed_model_calls"] == 1
    if omit_cache:
        assert report["cached_input_tokens"] is None
    else:
        assert report["cached_input_tokens"] == 960
