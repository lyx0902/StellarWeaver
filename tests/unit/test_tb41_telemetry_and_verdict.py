"""Regression tests for TB 4.1 verdict semantics, TrialLock strictness, and usage provenance."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stellarweaver.config import AppConfig, HarnessConfig, ModelConfig, PolicyConfig, RetryConfig
from stellarweaver.domain import AssistantReply, ToolCall
from stellarweaver.evals.terminal_bench import (
    HarborAgentContext,
    StellarWeaverHarborAgent,
    aggregate_trial_reports,
    build_terminal_agent,
    collect_harbor_job_reports,
    render_markdown_summary,
)
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent, CompletionProof
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.terminal import register_terminal_tools


@dataclass
class _MockExecResult:
    stdout: str
    stderr: str
    return_code: int


class _SequenceHarborEnv:
    def __init__(self, responses: dict[str, _MockExecResult]) -> None:
        self._responses = responses
        self.commands: list[str] = []

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> _MockExecResult:
        self.commands.append(command)
        return self._responses.get(command, _MockExecResult("", "", 0))


def _full_lock_entry(
    task_name: str,
    digest: str,
    agent_name: str,
    model_name: str,
    *,
    kwargs: dict[str, Any] | None = None,
    env_type: str = "docker",
    override_cpus: int | None = None,
    verifier_env_mode: str = "separate",
    verifier_disable: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "task": {
            "name": task_name,
            "version": "1.0.0",
            "type": "package",
            "digest": digest,
            "source": "terminal-bench/terminal-bench",
        },
        "install_only": False,
        "timeout_multiplier": 1.0,
        "agent": {
            "name": agent_name,
            "model_name": model_name,
            "skip_model_parsing": False,
            "skills": [],
            "resume_trajectory": False,
            "extra_allowed_hosts": [],
            "kwargs": {} if kwargs is None else kwargs,
            "mcp_servers": [],
        },
        "bridge_inputs": {},
        "skills": [],
        "environment": {
            "type": env_type,
            "force_build": False,
            "delete": False,
            "stream": False,
            "cpu_enforcement_policy": "auto",
            "memory_enforcement_policy": "auto",
            "override_cpus": override_cpus,
            "extra_docker_compose": [],
            "kwargs": {},
            "extra_allowed_hosts": [],
        },
        "verifier": {
            "disable": verifier_disable,
            "environment_mode": verifier_env_mode,
        },
    }


async def test_failed_command_followed_by_ls_does_not_overwrite_or_falsely_pass(
    tmp_path: Path,
) -> None:
    env = _SequenceHarborEnv(
        {
            "pytest -q": _MockExecResult("", "1 failed", 1),
            "ls": _MockExecResult("app.py\n", "", 0),
        }
    )

    def make_model() -> FakeModel:
        return FakeModel(
            [
                AssistantReply(
                    text="",
                    tool_calls=(ToolCall("c1", "bash", json.dumps({"command": "pytest -q"})),),
                    stop_reason="tool_calls",
                ),
                AssistantReply(
                    text="",
                    tool_calls=(ToolCall("c2", "bash", json.dumps({"command": "ls"})),),
                    stop_reason="tool_calls",
                ),
                AssistantReply(text="Done inspecting.", stop_reason="stop"),
            ]
        )

    custom_ws = tmp_path / "custom_ws"
    custom_ws.mkdir()
    engine = PolicyEngine("benchmark", workspace=custom_ws, allow_terminal_container=True)
    registry = ToolRegistry(custom_ws, engine, builtins=False, command_boundary="test-boundary")
    register_terminal_tools(registry, env)
    with SessionStore(custom_ws, data_dir=tmp_path / "custom_state") as store:
        custom_agent = Agent(
            make_model(),
            registry,
            store=store,
            harness_config=HarnessConfig(profile="custom"),
        )
        custom_outcome = await custom_agent.run("Run tests then ls")
    assert custom_outcome.run_status == "completed"
    assert custom_outcome.verification_status == "failed"

    tm_ws = tmp_path / "tm_ws"
    tm_ws.mkdir()
    cfg = AppConfig(
        model=ModelConfig(provider="fake", context_window_tokens=512_000),
        policy=PolicyConfig(preset="benchmark"),
        harness=HarnessConfig(profile="terminal-minimal"),
    )
    tm_engine = PolicyEngine("benchmark", workspace=tm_ws, allow_terminal_container=True)
    tm_registry = ToolRegistry(
        tm_ws, tm_engine, builtins=False, command_boundary="harbor-container-exec"
    )
    register_terminal_tools(tm_registry, env)
    with SessionStore(tm_ws, data_dir=tmp_path / "tm_state") as store:
        tm_agent = Agent(
            make_model(),
            tm_registry,
            store=store,
            harness_config=cfg.harness,
        )
        tm_outcome = await tm_agent.run("Run tests then ls")
    assert tm_outcome.run_status == "completed"
    assert tm_outcome.verification_status == "not_run"
    assert [e["kind"] for e in tm_outcome.evidence] == ["command", "command"]

    with SessionStore(tm_ws, data_dir=tmp_path / "tm_ls_only") as store:
        ls_agent = build_terminal_agent(
            cfg,
            FakeModel(
                [
                    AssistantReply(
                        text="",
                        tool_calls=(ToolCall("c3", "bash", json.dumps({"command": "ls"})),),
                        stop_reason="tool_calls",
                    ),
                    AssistantReply(text="Listed files.", stop_reason="stop"),
                ]
            ),
            store,
            env,
        )
        ls_outcome = await ls_agent.run("Run ls only")
    assert ls_outcome.verification_status == "not_run"

    async def pass_check(seconds: float) -> CompletionProof:
        assert math.isfinite(seconds) or math.isinf(seconds)
        return CompletionProof("passed", "verified", {"ok": True})

    with SessionStore(tm_ws, data_dir=tmp_path / "tm_proof") as store:
        proof_agent = Agent(
            FakeModel([AssistantReply(text="Finished.", stop_reason="stop")]),
            tm_registry,
            store=store,
            harness_config=cfg.harness,
            completion_check=pass_check,
        )
        proof_outcome = await proof_agent.run("Check completion proof")
    assert proof_outcome.verification_status == "passed"


def test_unplanned_receipts_excluded_from_metrics_and_corrupt_lock_flagged(tmp_path: Path) -> None:
    job_dir = tmp_path / "unplanned_job"
    job_dir.mkdir()
    planned_entry = _full_lock_entry(
        "terminal-bench/task-a",
        "sha256:" + "a" * 64,
        "stellarweaver",
        "deepseek/flash",
    )
    (job_dir / "lock.json").write_text(json.dumps({"trials": [planned_entry]}), encoding="utf-8")

    # Planned trial failed
    t1 = job_dir / "task-a__1"
    (t1 / "agent").mkdir(parents=True)
    (t1 / "lock.json").write_text(json.dumps(planned_entry), encoding="utf-8")
    (t1 / "result.json").write_text(
        json.dumps(
            {
                "task_name": "terminal-bench/task-a",
                "trial_name": "task-a__1",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (t1 / "agent" / "trial-report.json").write_text(
        json.dumps(
            {
                "usage_accounting_version": 2,
                "reported_input_tokens": 1000,
                "reported_output_tokens": 100,
                "reported_cached_input_tokens": 800,
                "reported_estimated_cost_usd": 0.001,
                "input_usage_complete": True,
                "output_usage_complete": True,
                "cache_usage_complete": True,
            }
        ),
        encoding="utf-8",
    )

    # Extra unplanned trial passed
    t2 = job_dir / "extra-task__2"
    (t2 / "agent").mkdir(parents=True)
    (t2 / "result.json").write_text(
        json.dumps(
            {
                "task_name": "terminal-bench/extra-task",
                "trial_name": "extra-task__2",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    (t2 / "agent" / "trial-report.json").write_text(
        json.dumps(
            {
                "usage_accounting_version": 2,
                "reported_input_tokens": 9000,
                "reported_output_tokens": 900,
                "reported_cached_input_tokens": 900,
                "reported_estimated_cost_usd": 0.009,
                "input_usage_complete": True,
                "output_usage_complete": True,
                "cache_usage_complete": True,
            }
        ),
        encoding="utf-8",
    )

    reports = collect_harbor_job_reports(job_dir)
    assert len(reports) == 2
    assert reports[0]["planned"] is True
    assert reports[1]["planned"] is False
    assert reports[1]["receipt_status"] == "unplanned"

    agg = aggregate_trial_reports(reports)
    assert agg["tasks"] == 1
    assert agg["passed"] == 0
    assert agg["failed"] == 1
    assert agg["unplanned"] == 1
    assert agg["resolved_rate"] == 0.0
    assert agg["total_reported_input_tokens"] == 1000

    # Corrupt lock.json -> must NOT silently pass planned denominator checks
    corrupt_dir = tmp_path / "corrupt_job"
    corrupt_dir.mkdir()
    (corrupt_dir / "lock.json").write_text("{not valid json", encoding="utf-8")
    ct1 = corrupt_dir / "task-a__1"
    ct1.mkdir()
    (ct1 / "result.json").write_text(
        json.dumps({"task_name": "task-a", "verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )
    corrupt_reports = collect_harbor_job_reports(corrupt_dir)
    assert corrupt_reports[0]["lock_status"] == "corrupt"
    assert corrupt_reports[0]["receipt_status"] == "lock_unverifiable"
    corrupt_agg = aggregate_trial_reports(corrupt_reports)
    assert corrupt_agg["planned_denominator_verifiable"] is False
    assert corrupt_agg["resolved_rate"] is None
    assert "unverifiable (corrupt lock.json)" in render_markdown_summary(corrupt_reports)


def test_strict_trial_lock_matching_rejects_digest_env_verifier_and_kwargs_mismatches(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "strict_lock_job"
    job_dir.mkdir()
    planned = _full_lock_entry(
        "terminal-bench/task-x",
        "sha256:" + "1" * 64,
        "stellarweaver",
        "deepseek/flash",
        kwargs={"reasoning_effort": "max"},
        override_cpus=2,
        verifier_env_mode="separate",
    )
    planned_dup = _full_lock_entry(
        "terminal-bench/task-x",
        "sha256:" + "1" * 64,
        "stellarweaver",
        "deepseek/flash",
        kwargs={"reasoning_effort": "max"},
        override_cpus=2,
        verifier_env_mode="separate",
    )
    (job_dir / "lock.json").write_text(
        json.dumps({"trials": [planned, planned_dup]}), encoding="utf-8"
    )

    def write_trial_with_lock(name: str, lock_payload: dict[str, Any], reward: float) -> None:
        tdir = job_dir / name
        tdir.mkdir()
        (tdir / "lock.json").write_text(json.dumps(lock_payload), encoding="utf-8")
        (tdir / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "terminal-bench/task-x",
                    "trial_name": name,
                    "verifier_result": {"rewards": {"reward": reward}},
                }
            ),
            encoding="utf-8",
        )

    # Exact match for slot 0
    write_trial_with_lock("exact_match", planned, 1.0)
    # Differing digest (v2 vs v1)
    write_trial_with_lock(
        "wrong_digest",
        _full_lock_entry(
            "terminal-bench/task-x",
            "sha256:" + "2" * 64,
            "stellarweaver",
            "deepseek/flash",
            kwargs={"reasoning_effort": "max"},
            override_cpus=2,
        ),
        1.0,
    )
    # Differing environment (override_cpus=4 vs 2)
    write_trial_with_lock(
        "wrong_env",
        _full_lock_entry(
            "terminal-bench/task-x",
            "sha256:" + "1" * 64,
            "stellarweaver",
            "deepseek/flash",
            kwargs={"reasoning_effort": "max"},
            override_cpus=4,
        ),
        1.0,
    )
    # Differing verifier (shared vs separate)
    write_trial_with_lock(
        "wrong_verifier",
        _full_lock_entry(
            "terminal-bench/task-x",
            "sha256:" + "1" * 64,
            "stellarweaver",
            "deepseek/flash",
            kwargs={"reasoning_effort": "max"},
            override_cpus=2,
            verifier_env_mode="shared",
        ),
        1.0,
    )
    # Explicit empty kwargs={} vs {"reasoning_effort": "max"}
    write_trial_with_lock(
        "wrong_kwargs",
        _full_lock_entry(
            "terminal-bench/task-x",
            "sha256:" + "1" * 64,
            "stellarweaver",
            "deepseek/flash",
            kwargs={},
            override_cpus=2,
        ),
        1.0,
    )

    reports = collect_harbor_job_reports(job_dir)
    planned_only = [r for r in reports if r["planned"] is True]
    unplanned_only = [r for r in reports if r["planned"] is False]
    assert len(planned_only) == 2
    assert planned_only[0]["trial_name"] == "exact_match"
    assert planned_only[0]["verdict"] == "passed"
    assert planned_only[1]["receipt_status"] == "missing_receipt"
    assert planned_only[1]["verdict"] == "unscored"
    assert len(unplanned_only) == 4  # none of the mismatched locks may claim slot 1!

    agg = aggregate_trial_reports(reports)
    assert agg["tasks"] == 2
    assert agg["passed"] == 1
    assert agg["unscored"] == 1
    assert agg["unplanned"] == 4
    assert agg["resolved_rate"] == 0.5


def test_job_pending_trials_is_job_level_and_order_invariant(tmp_path: Path) -> None:
    entry_a = _full_lock_entry(
        "terminal-bench/task-a", "sha256:" + "a" * 64, "sw", "deepseek/flash"
    )
    entry_b = _full_lock_entry(
        "terminal-bench/task-b", "sha256:" + "b" * 64, "sw", "deepseek/flash"
    )

    def run_with_order(name: str, order: list[dict[str, Any]], pending: int) -> list[dict[str, Any]]:
        jdir = tmp_path / name
        jdir.mkdir()
        (jdir / "lock.json").write_text(json.dumps({"trials": order}), encoding="utf-8")
        (jdir / "result.json").write_text(
            json.dumps({"stats": {"n_pending_trials": pending}}), encoding="utf-8"
        )
        return collect_harbor_job_reports(jdir)

    rep_ab = run_with_order("ab", [entry_a, entry_b], pending=1)
    rep_ba = run_with_order("ba", [entry_b, entry_a], pending=2)
    by_task_ab = {r["task_name"]: r["receipt_status"] for r in rep_ab}
    by_task_ba = {r["task_name"]: r["receipt_status"] for r in rep_ba}
    assert by_task_ab == by_task_ba == {
        "terminal-bench/task-a": "missing_receipt",
        "terminal-bench/task-b": "missing_receipt",
    }
    assert aggregate_trial_reports(rep_ab)["job_pending_trials"] == 1
    assert aggregate_trial_reports(rep_ba)["job_pending_trials"] == 2


async def test_per_metric_usage_source_classification_and_completeness(tmp_path: Path) -> None:
    env = _SequenceHarborEnv({})

    # Case 1: completed without usage + failed WITH provider error.usage
    class _InvertedUsageProvider:
        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self) -> Any:
            from stellarweaver.domain import ModelCapabilities

            return ModelCapabilities(tool_calling=True, streaming=False, context_window_tokens=512_000)

        async def complete(self, request: Any) -> AssistantReply:
            self.calls += 1
            if self.calls == 1:
                raise ProviderError(
                    "rate limit with reported usage",
                    retryable=True,
                    usage={"prompt_tokens": 500, "completion_tokens": 20, "prompt_cache_hit_tokens": 300},
                )
            return AssistantReply(text="Done without usage.", stop_reason="stop", usage=None)

    cfg = AppConfig(
        model=ModelConfig(supports_streaming=False, context_window_tokens=512_000),
        policy=PolicyConfig(preset="benchmark"),
        retry=RetryConfig(max_retries=2, base_delay_seconds=0.0, max_delay_seconds=1.0),
        harness=HarnessConfig(profile="terminal-minimal"),
    )
    ctx1 = HarborAgentContext()
    rep1 = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "case1", config=cfg, provider=_InvertedUsageProvider(), stream=False
    ).run("Task", env, ctx1)

    # Provider usage from failed call goes to reported_*; missing usage on completed call goes to estimated_*!
    assert rep1["reported_input_tokens"] == 500
    assert rep1["reported_output_tokens"] == 20
    assert rep1["reported_cached_input_tokens"] == 300
    assert rep1["estimated_input_tokens"] > 0
    assert rep1["estimated_output_tokens"] > 0
    assert rep1["failed_estimated_input_tokens"] == 0
    assert rep1["input_usage_complete"] is False
    assert rep1["output_usage_complete"] is False
    assert rep1["cache_usage_complete"] is False
    assert rep1["cache_hit_ratio"] is None
    assert rep1["reported_estimated_cost_usd"] is None
    assert ctx1.n_input_tokens is None
    assert ctx1.n_output_tokens is None
    assert ctx1.cost_usd is None
    assert ctx1.model_usage is None

    # Case 2: input + cache complete on completed call, output missing -> cache_hit_ratio computed, cost incomplete!
    class _InputOnlyUsageProvider:
        def capabilities(self) -> Any:
            from stellarweaver.domain import ModelCapabilities

            return ModelCapabilities(tool_calling=True, streaming=False, context_window_tokens=512_000)

        async def complete(self, request: Any) -> AssistantReply:
            return AssistantReply(
                text="Input reported only.",
                stop_reason="stop",
                usage={"prompt_tokens": 1000, "prompt_cache_hit_tokens": 750},
            )

    ctx2 = HarborAgentContext()
    rep2 = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "case2", config=cfg, provider=_InputOnlyUsageProvider(), stream=False
    ).run("Task", env, ctx2)

    assert rep2["input_usage_complete"] is True
    assert rep2["cache_usage_complete"] is True
    assert rep2["output_usage_complete"] is False
    assert rep2["reported_input_tokens"] == 1000
    assert rep2["reported_cached_input_tokens"] == 750
    assert rep2["cache_hit_ratio"] == 0.75
    assert rep2["reported_estimated_cost_usd"] is None
    assert ctx2.n_input_tokens == 1000
    assert ctx2.n_cache_tokens == 750
    assert ctx2.n_output_tokens is None
    assert ctx2.cost_usd is None
    assert ctx2.model_usage is None


def test_legacy_receipt_sqlite_readback_and_unverified_isolation(tmp_path: Path) -> None:
    # 1. Valid legacy trial with WAL SQLite containing reported completed + ambiguous legacy failed event.
    job_dir = tmp_path / "legacy_job"
    tdir = job_dir / "legacy_with_db"
    sw_dir = tdir / "agent" / "sw"
    sw_dir.mkdir(parents=True)
    ws_dir = tmp_path / "ws_dummy"
    ws_dir.mkdir()
    with SessionStore(ws_dir, data_dir=sw_dir) as store:
        sid = store.session_id
        rid = store.run_id
        store.append_event(
            "model.completed",
            {"input_tokens": 1000, "input_estimated": False, "output_tokens": 120, "estimated": False, "prompt_cache_hit_tokens": 800},
        )
        # Legacy failed event: has input_estimated=True & estimated=True, lacks output_estimated and usage.
        store.append_event(
            "model.failed",
            {"input_tokens": 900, "input_estimated": True, "output_tokens": 65536, "estimated": True},
        )
        rel_db = store.path.relative_to(tdir / "agent").as_posix()

    (tdir / "result.json").write_text(
        json.dumps({"task_name": "legacy-ok", "verifier_result": {"rewards": {"reward": 1.0}}}),
        encoding="utf-8",
    )
    (tdir / "agent" / "trial-report.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "session_db_relpath": rel_db,
                "model": "deepseek-flash",
                "profile": "terminal-minimal",
                "outcome": {"run_id": rid, "input_tokens": 1900, "output_tokens": 65656},
                "cached_input_tokens": 800,
                "estimated_cost_usd": 0.08,
            }
        ),
        encoding="utf-8",
    )

    rebuilt_reports = collect_harbor_job_reports(job_dir)
    rep = rebuilt_reports[0]
    assert rep["usage_accounting_source"] == "legacy_sqlite_rebuilt"
    assert rep["reported_input_tokens"] == 1000
    assert rep["reported_output_tokens"] == 120
    assert rep["reported_cached_input_tokens"] == 800
    assert rep["failed_estimated_input_tokens"] == 900
    # Ambiguous legacy failed output MUST stay unknown (neither reported nor failed_estimated)!
    assert rep["unknown_output_tokens"] == 65536
    assert rep["failed_estimated_output_tokens"] == 0

    # 2. Legacy report without valid SQLite (or path escape attempt) stays legacy_mixed_unverified.
    bad_dir = tmp_path / "legacy_no_db" / "trial1"
    (bad_dir / "agent").mkdir(parents=True)
    (bad_dir / "result.json").write_text(
        json.dumps({"task_name": "legacy-bad", "verifier_result": {"rewards": {"reward": 0.0}}}),
        encoding="utf-8",
    )
    (bad_dir / "agent" / "trial-report.json").write_text(
        json.dumps(
            {
                "session_id": "missing",
                "session_db_relpath": "../../outside.sqlite3",
                "model": "deepseek-flash",
                "profile": "terminal-minimal",
                "outcome": {"run_id": "r1", "input_tokens": 5000, "output_tokens": 200},
                "cached_input_tokens": 3000,
                "estimated_cost_usd": 0.05,
            }
        ),
        encoding="utf-8",
    )
    bad_reports = collect_harbor_job_reports(tmp_path / "legacy_no_db")
    bad_rep = bad_reports[0]
    assert bad_rep["usage_accounting_source"] == "legacy_mixed_unverified"
    assert bad_rep["reported_input_tokens"] is None
    bad_agg = aggregate_trial_reports(bad_reports)
    assert bad_agg["total_reported_input_tokens"] == 0
    assert bad_agg["legacy_mixed_trials"] == 1
    assert bad_agg["total_legacy_mixed_input_tokens"] == 5000
    assert "| Legacy Mixed Trials | 1 |" in render_markdown_summary(bad_reports)

    # populate_context_post_run must not feed unverified legacy mixed tokens into Harbor context.
    ctx_legacy = HarborAgentContext()
    StellarWeaverHarborAgent(logs_dir=bad_dir / "agent", stream=False).populate_context_post_run(
        ctx_legacy
    )
    assert ctx_legacy.n_input_tokens is None
    assert ctx_legacy.n_output_tokens is None
    assert ctx_legacy.cost_usd is None
    assert ctx_legacy.model_usage is None


def test_collect_harbor_job_reports_parses_optional_ctrf_diagnostics(tmp_path: Path) -> None:
    job_dir = tmp_path / "ctrf_job"
    t1 = job_dir / "with_ctrf"
    (t1 / "verifier").mkdir(parents=True)
    (t1 / "result.json").write_text(
        json.dumps(
            {
                "task_name": "vf2",
                "trial_name": "with_ctrf",
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    (t1 / "verifier" / "ctrf.json").write_text(
        json.dumps({"results": {"summary": {"tests": 60, "passed": 59, "failed": 1, "skipped": 2}}}),
        encoding="utf-8",
    )

    t2 = job_dir / "without_ctrf"
    t2.mkdir(parents=True)
    (t2 / "result.json").write_text(
        json.dumps(
            {
                "task_name": "kv",
                "trial_name": "without_ctrf",
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        ),
        encoding="utf-8",
    )

    reports = {r["trial_name"]: r for r in collect_harbor_job_reports(job_dir)}
    assert reports["with_ctrf"]["verdict"] == "failed"
    assert reports["with_ctrf"]["ctrf_passed"] == 59
    assert reports["with_ctrf"]["ctrf_total"] == 60
    assert reports["with_ctrf"]["ctrf_skipped"] == 2
    assert reports["with_ctrf"]["ctrf_summary"] == {
        "total": 60,
        "passed": 59,
        "failed": 1,
        "skipped": 2,
    }
    assert reports["without_ctrf"]["verdict"] == "passed"
    assert reports["without_ctrf"]["ctrf_summary"] is None
