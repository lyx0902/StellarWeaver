"""Offline unit tests for root lock proof, Docker memory preflight, error denoising, and PrefixCacheAuditor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stellarweaver.config import AppConfig, HarnessConfig, LimitsConfig, ModelConfig, PolicyConfig
from stellarweaver.context import PrefixCacheAuditor
from stellarweaver.domain import AssistantReply, Message, ModelRequest, ToolCall, ToolResult
from stellarweaver.evals.terminal_bench import (
    StellarWeaverHarborAgent,
    aggregate_trial_reports,
    collect_harbor_job_reports,
    launch_harbor_with_preflight,
    render_markdown_summary,
    resolve_task_memory_spec,
    run_docker_memory_preflight,
    summarize_harbor_job,
)
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent, normalize_error_text, stable_failure
from stellarweaver.runtime.repair import RepairController
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.terminal import TerminalExecResult, register_terminal_tools


def _write_v2_trial(
    job_dir: Path,
    trial_name: str,
    *,
    task_name: str,
    reward: float,
    lock_entry: dict[str, Any] | None = None,
) -> None:
    tdir = job_dir / trial_name
    (tdir / "agent").mkdir(parents=True)
    if lock_entry is not None:
        (tdir / "lock.json").write_text(json.dumps(lock_entry), encoding="utf-8")
    (tdir / "result.json").write_text(
        json.dumps(
            {
                "task_name": task_name,
                "trial_name": trial_name,
                "verifier_result": {"rewards": {"reward": reward}},
            }
        ),
        encoding="utf-8",
    )
    (tdir / "agent" / "trial-report.json").write_text(
        json.dumps(
            {
                "usage_accounting_version": 2,
                "reported_input_tokens": 1000,
                "reported_output_tokens": 100,
                "reported_cached_input_tokens": 900,
                "reported_estimated_cost_usd": 0.001,
                "input_usage_complete": True,
                "output_usage_complete": True,
                "cache_usage_complete": True,
            }
        ),
        encoding="utf-8",
    )


def _sample_lock_entry(task_name: str, digest: str) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "task": {"name": task_name, "type": "package", "digest": digest},
        "timeout_multiplier": 1.0,
        "agent": {"name": "stellarweaver", "model_name": "deepseek/flash", "kwargs": {}},
        "environment": {"type": "docker", "delete": True},
        "verifier": {"disable": False, "environment_mode": "separate"},
    }


def test_root_lock_absent_empty_and_partial_receipts(tmp_path: Path) -> None:
    empty_job = tmp_path / "empty_no_lock"
    empty_job.mkdir()
    empty_summary = summarize_harbor_job(empty_job)
    assert empty_summary["lock_status"] == "absent"
    assert empty_summary["planned_denominator_verifiable"] is False
    assert empty_summary["batch_complete"] is False
    assert empty_summary["summary"]["tasks"] is None
    assert empty_summary["summary"]["observed_receipts"] == 0
    assert empty_summary["summary"]["resolved_rate"] is None
    assert "missing lock.json" in empty_summary["markdown"]
    assert "| Tasks | unknown |" in empty_summary["markdown"]

    partial_job = tmp_path / "partial_no_lock"
    partial_job.mkdir()
    _write_v2_trial(partial_job, "t1", task_name="terminal-bench/a", reward=1.0)
    _write_v2_trial(partial_job, "t2", task_name="terminal-bench/b", reward=1.0)
    partial_summary = summarize_harbor_job(partial_job)
    assert partial_summary["lock_status"] == "absent"
    assert partial_summary["planned_denominator_verifiable"] is False
    assert partial_summary["summary"]["tasks"] is None
    assert partial_summary["summary"]["observed_receipts"] == 2
    assert partial_summary["summary"]["passed"] == 2
    assert partial_summary["summary"]["resolved_rate"] is None
    assert "2 observed (rate unknown; missing lock.json)" in partial_summary["markdown"]


def test_root_lock_valid_interrupted_and_invalid_entries(tmp_path: Path) -> None:
    interrupted = tmp_path / "interrupted_valid"
    interrupted.mkdir()
    planned = [
        _sample_lock_entry(f"terminal-bench/task-{idx}", f"sha256:{str(idx) * 64}")
        for idx in range(4)
    ]
    (interrupted / "lock.json").write_text(json.dumps({"trials": planned}), encoding="utf-8")
    _write_v2_trial(
        interrupted, "task-0__1", task_name="terminal-bench/task-0", reward=1.0, lock_entry=planned[0]
    )
    _write_v2_trial(
        interrupted, "task-1__2", task_name="terminal-bench/task-1", reward=0.0, lock_entry=planned[1]
    )

    summary = summarize_harbor_job(interrupted)
    assert summary["lock_status"] == "valid"
    assert summary["planned_denominator_verifiable"] is True
    assert summary["batch_complete"] is False
    assert summary["summary"]["tasks"] == 4
    assert summary["summary"]["observed_receipts"] == 2
    assert summary["summary"]["missing_receipt"] == 2
    assert summary["summary"]["passed"] == 1
    assert summary["summary"]["resolved_rate"] == 0.25
    assert "| Observed Receipts | 2 / 4 |" in summary["markdown"]
    assert "| Batch Complete | no |" in summary["markdown"]

    # Empty trials list in root lock.json must be treated as corrupt
    empty_trials_job = tmp_path / "empty_trials_lock"
    empty_trials_job.mkdir()
    (empty_trials_job / "lock.json").write_text(json.dumps({"trials": []}), encoding="utf-8")
    empty_reports = collect_harbor_job_reports(empty_trials_job)
    empty_agg = aggregate_trial_reports(empty_reports)
    assert empty_agg["lock_status"] == "corrupt"
    assert empty_agg["planned_denominator_verifiable"] is False
    assert empty_agg["resolved_rate"] is None
    assert "unverifiable (corrupt lock.json)" in render_markdown_summary(empty_reports)

    # Trial entry lacking task identity in lock.json must be treated as corrupt
    no_id_job = tmp_path / "no_identity_lock"
    no_id_job.mkdir()
    (no_id_job / "lock.json").write_text(
        json.dumps({"trials": [{"task": {}, "agent": {"name": "stellarweaver"}}]}),
        encoding="utf-8",
    )
    no_id_agg = aggregate_trial_reports(collect_harbor_job_reports(no_id_job))
    assert no_id_agg["lock_status"] == "corrupt"
    assert no_id_agg["planned_denominator_verifiable"] is False
    assert "identity" in str(no_id_agg["lock_reason"])


def test_docker_memory_preflight_blocks_low_mem_and_verifier_exceedance(tmp_path: Path) -> None:
    fast19_yaml = Path("deploy/harbor-tb41-fast19.yaml")

    # 7.52 GiB (previous local Docker VM capacity) must fail on the 7 x 8192 MiB tasks
    low_report = run_docker_memory_preflight(
        fast19_yaml,
        docker_mem_bytes_fn=lambda: 8_073_670_656,
        cache_root=tmp_path / "empty_cache",
    )
    assert low_report["ok"] is False
    assert low_report["tasks_checked"] == 19
    assert len(low_report["exceeded"]) == 7
    assert {item["task"] for item in low_report["exceeded"]} == {
        "terminal-bench/cad-model",
        "terminal-bench/hof-topology-interpenetration",
        "terminal-bench/html-js-filter",
        "terminal-bench/satb-audio-transcription",
        "terminal-bench/telecom-entity-resolution",
        "terminal-bench/vba-userform-port",
        "terminal-bench/vpp-loss-divergence",
    }

    # 10 GiB passes cleanly
    ok_report = run_docker_memory_preflight(
        fast19_yaml,
        docker_mem_bytes_fn=lambda: 10 * 1024**3,
        cache_root=tmp_path / "empty_cache",
    )
    assert ok_report["ok"] is True
    assert ok_report["exceeded"] == []

    # Verifier-only exceedance (agent=4096, separate verifier=8192)
    cache_dir = tmp_path / "cache" / "terminal-bench" / "ver-heavy" / ("a" * 64)
    cache_dir.mkdir(parents=True)
    (cache_dir / "task.toml").write_text(
        """
[verifier]
environment_mode = "separate"

[verifier.environment]
memory_mb = 8192

[environment]
memory_mb = 4096
""",
        encoding="utf-8",
    )
    ver_yaml = tmp_path / "ver_only.yaml"
    ver_yaml.write_text(
        """
environment:
  type: docker
datasets:
  - name: terminal-bench/terminal-bench
    task_names:
      - terminal-bench/ver-heavy
""",
        encoding="utf-8",
    )
    ver_report = run_docker_memory_preflight(
        ver_yaml,
        docker_mem_bytes_fn=lambda: 8_073_670_656,
        cache_root=tmp_path / "cache",
    )
    assert ver_report["ok"] is False
    assert len(ver_report["exceeded"]) == 1
    assert ver_report["exceeded"][0]["sides"] == ["verifier"]

    # Docker unreachable
    unreachable = run_docker_memory_preflight(
        fast19_yaml,
        docker_mem_bytes_fn=lambda: (_ for _ in ()).throw(RuntimeError("daemon not running")),
        cache_root=tmp_path / "empty_cache",
    )
    assert unreachable["ok"] is False
    assert unreachable["docker_reachable"] is False

    # Digest mismatch prevents stale manifest reuse
    mismatch = resolve_task_memory_spec(
        "terminal-bench/cad-model",
        expected_digest="sha256:" + "0" * 64,
        cache_root=tmp_path / "empty_cache",
    )
    assert mismatch["status"] == "unverified"
    assert mismatch["reason"] == "digest_mismatch"

    # launch_harbor_with_preflight must NOT invoke Harbor when preflight blocks
    calls: list[Any] = []
    exit_code = launch_harbor_with_preflight(
        fast19_yaml,
        preflight_fn=lambda _p: low_report,
        runner_fn=lambda *a, **kw: calls.append((a, kw)),
    )
    assert exit_code == 2
    assert calls == []


class _NoisyBashEnv:
    def __init__(self) -> None:
        self.count = 0

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> TerminalExecResult:
        self.count += 1
        return TerminalExecResult(
            stdout="",
            stderr=(
                f"2026-11-27T11:00:0{self.count}Z error at 0x7f12abcd{self.count:04x} "
                f"in /tmp/run_{self.count}/case.py pid={1000 + self.count} elapsed=0.{self.count}2s"
            ),
            return_code=1,
        )


async def test_error_denoising_and_repair_command_key(tmp_path: Path) -> None:
    sample_a = "2026-11-27T11:00:01Z segfault at 0x7f00dead1234 in /tmp/abc123/test.py pid=421 1.25s"
    sample_b = "2026-11-27T11:05:09Z segfault at 0x7fffbeef9999 in /tmp/xyz999/other.py pid=998 3.81s"
    assert normalize_error_text(sample_a) == normalize_error_text(sample_b)
    assert stable_failure({"stderr": sample_a, "operation_id": "op1"}) == stable_failure(
        {"stderr": sample_b, "operation_id": "op2"}
    )

    # RepairController must distinguish bash calls via data["command"]
    repair = RepairController(enforce=False)
    ev1, _ = repair.observe(
        ToolResult("op1", "c1", "bash", "error", "", {"command": "pytest -q", "exit_code": 1}),
        command=True,
        hard_threshold=3,
    )
    ev2, _ = repair.observe(
        ToolResult("op2", "c2", "bash", "error", "", {"command": "cargo test", "exit_code": 1}),
        command=True,
        hard_threshold=3,
    )
    assert ev1 is not None and ev2 is not None
    assert ev1["repeat_count"] == 1
    assert ev2["repeat_count"] == 1

    # End-to-end Agent loop triggers soft guidance (3) and hard stop (6) despite volatile noise
    replies = [
        AssistantReply(
            text="",
            tool_calls=(ToolCall(f"c{i}", "bash", json.dumps({"command": "pytest -q"})),),
            stop_reason="tool_calls",
        )
        for i in range(1, 8)
    ]
    ws = tmp_path / "ws"
    ws.mkdir()
    engine = PolicyEngine("benchmark", workspace=ws, allow_terminal_container=True)
    registry = ToolRegistry(ws, engine, builtins=False, command_boundary="harbor-container-exec")
    register_terminal_tools(registry, _NoisyBashEnv())
    cfg = AppConfig(
        model=ModelConfig(provider="fake", context_window_tokens=128_000),
        limits=LimitsConfig(max_model_calls=20, max_tool_calls=20, max_wall_seconds=60),
        policy=PolicyConfig(preset="benchmark"),
        harness=HarnessConfig(
            profile="terminal-minimal",
            repeated_failure_soft_threshold=3,
            repeated_failure_hard_threshold=6,
            benchmark_allow_denied_tool_continue=True,
        ),
    )
    events = []
    with SessionStore(ws, data_dir=tmp_path / "state") as store:
        agent = Agent(
            FakeModel(replies),
            registry,
            limits=cfg.limits,
            harness_config=cfg.harness,
            store=store,
            on_event=events.append,
        )
        outcome = await agent.run("Fix the noisy test")
    assert outcome.run_status == "failed"
    assert "6 identical tool failures" in outcome.reason
    assert any(e.type == "repair.soft_guidance" for e in events)


def test_prefix_cache_auditor_tracks_append_compaction_and_breaks() -> None:
    auditor = PrefixCacheAuditor()
    tools = ({"type": "function", "function": {"name": "bash"}},)
    sys_msg = Message("system", "SYS")
    sys_mutated = Message("system", "SYS_MUTATED")
    m1 = Message("user", "Task prompt")
    m2 = Message("assistant", "Checking environment")

    req1 = ModelRequest((sys_msg, m1), tools=tools)
    req2 = ModelRequest((sys_msg, m1, m2), tools=tools)
    assert auditor.audit(req1, model_key="k1", compacted=False, request_index=1) is None
    assert auditor.audit(req2, model_key="k1", compacted=False, request_index=2) is None

    # Planned compaction turn is allowed to rewrite earlier messages without raising divergence
    req3 = ModelRequest((sys_msg, Message("user", "Compacted summary"), m2), tools=tools)
    assert auditor.audit(req3, model_key="k1", compacted=True, request_index=3) is None

    # Unexpected system prompt modification without compaction triggers precise diff
    req4 = ModelRequest((sys_mutated, Message("user", "Compacted summary"), m2), tools=tools)
    div = auditor.audit(req4, model_key="k1", compacted=False, request_index=4)
    assert div is not None
    assert div["first_diff_section"] == "system"
    assert "SYS" in div["prev_sample"]
    assert "SYS_MUTATED" in div["curr_sample"]


async def test_harbor_agent_records_unexpected_prefix_divergences(tmp_path: Path) -> None:
    class _MutatingProvider(FakeModel):
        pass

    env = _NoisyBashEnv()
    logs_dir = tmp_path / "agent_logs"
    # Normal append-only Harbor trial has 0 unexpected_prefix_divergences
    agent = StellarWeaverHarborAgent(
        logs_dir=logs_dir,
        provider=_MutatingProvider(
            [
                AssistantReply(
                    text="",
                    tool_calls=(ToolCall("c1", "bash", json.dumps({"command": "pytest -q"})),),
                    stop_reason="tool_calls",
                ),
                AssistantReply(text="Done", stop_reason="stop"),
            ]
        ),
        stream=False,
    )
    report = await agent.run("Inspect repo", env)
    assert report["unexpected_prefix_divergences"] == 0
