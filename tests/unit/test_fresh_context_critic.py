"""Offline tests for Fresh-Context Adversarial Critic, receipt gating, budget reserves, and Harbor telemetry."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import asdict
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from stellarweaver.config import (
    AppConfig,
    ContextConfig,
    LimitsConfig,
    ModelConfig,
    terminal_minimal_harness,
)
from stellarweaver.domain import (
    AgentEvent,
    AssistantReply,
    Message,
    ModelRequest,
    ModelStreamEvent,
    ToolCall,
    ToolResult,
)
from stellarweaver.evals.terminal_bench import (
    StellarWeaverHarborAgent,
    _summarize_usage_events,
    aggregate_trial_reports,
    render_markdown_summary,
    summarize_harbor_job,
)
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.critic import (
    _CONTAINER_PRE_PROBE_SCRIPT,
    CRITIC_SCHEMA_VERSION,
    PARENT_RESERVE_MODEL_CALLS,
    SubmitCritiqueArgs,
    _build_post_probe_script,
    _diff_snapshots,
    _parse_probe_json,
    _validate_critique_receipts,
    run_fresh_context_critic,
)
from stellarweaver.runtime.loop import Agent, CriticHandoff
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.terminal import TerminalExecResult, register_terminal_tools


class _MockContainerEnv:
    """Fake Harbor container executor separating probe/cleanup from user/critic bash calls."""

    def __init__(
        self,
        bash_outputs: list[TerminalExecResult] | None = None,
        *,
        pre_snapshot: dict[str, list[Any]] | None = None,
        post_snapshot: dict[str, list[Any]] | None = None,
        inventory: str = "[git status --short]\n M app/calc.py",
        pre_probe_override: TerminalExecResult | None = None,
        post_probe_override: TerminalExecResult | None = None,
        cleanup_exit_code: int = 0,
        cancel_on_pre_probe: bool = False,
    ) -> None:
        self._outputs = list(bash_outputs or [])
        self.pre_snapshot = (
            dict(pre_snapshot)
            if pre_snapshot is not None
            else {"/app/calc.py": [128, 1000, "abc"]}
        )
        self.post_snapshot = (
            dict(post_snapshot) if post_snapshot is not None else dict(self.pre_snapshot)
        )
        self.inventory = inventory
        self.pre_probe_override = pre_probe_override
        self.post_probe_override = post_probe_override
        self.cleanup_exit_code = cleanup_exit_code
        self.cancel_on_pre_probe = cancel_on_pre_probe
        self.post_probe_commands: list[str] = []
        self.bash_commands: list[dict[str, Any]] = []
        self.cleanup_commands: list[str] = []

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> TerminalExecResult:
        if "__SW_CRITIC_SNAPSHOT_JSON__" in command:
            if self.cancel_on_pre_probe:
                raise asyncio.CancelledError("cancelled during pre-probe")
            if self.pre_probe_override is not None:
                return self.pre_probe_override
            payload = json.dumps(
                {
                    "roots": ["/app"],
                    "files": self.pre_snapshot,
                    "truncated": False,
                    "read_errors": 0,
                    "missing_roots": [],
                }
            )
            return TerminalExecResult(
                stdout=f"{self.inventory}\n__SW_CRITIC_SNAPSHOT_JSON__\n{payload}\n",
                return_code=0,
            )
        if "__SW_POST__" in command:
            self.post_probe_commands.append(command)
            if self.post_probe_override is not None:
                return self.post_probe_override
            payload = json.dumps(
                {
                    "roots": ["/app"],
                    "files": self.post_snapshot,
                    "truncated": False,
                    "read_errors": 0,
                    "missing_roots": [],
                }
            )
            return TerminalExecResult(stdout=f"{payload}\n", return_code=0)
        if command.startswith("rm -rf /tmp/.sw_critic_"):
            self.cleanup_commands.append(command)
            return TerminalExecResult(return_code=self.cleanup_exit_code)
        self.bash_commands.append(
            {"command": command, "cwd": cwd, "env": dict(env or {}), "timeout_sec": timeout_sec}
        )
        if self._outputs:
            return self._outputs.pop(0)
        return TerminalExecResult(stdout="ok\n", return_code=0)


def _usage(prompt: int, cached: int, completion: int) -> dict[str, int]:
    return {
        "prompt_tokens": prompt,
        "prompt_cache_hit_tokens": cached,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _submit_critique_call(
    call_id: str,
    *,
    status: str = "pass",
    summary: str = "Verified requirements",
    findings: list[dict[str, Any]] | None = None,
) -> ToolCall:
    return ToolCall(
        call_id,
        "submit_critique",
        json.dumps(
            {
                "status": status,
                "summary": summary,
                "findings": findings or [],
                "verified_requirements": ["core formula"],
                "unverified_risks": [],
            }
        ),
    )


def test_harbor_mutual_exclusion_and_manifest_fingerprints(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        StellarWeaverHarborAgent(
            logs_dir=tmp_path / "conflict_bool",
            completion_evidence_reminder=True,
            fresh_context_critic=True,
        )

    with pytest.raises(ValidationError, match="mutually exclusive"):
        StellarWeaverHarborAgent(
            logs_dir=tmp_path / "conflict_str",
            completion_evidence_reminder="true",
            fresh_context_critic="yes",
        )

    with pytest.raises(ValueError, match="fresh_context_critic_mode"):
        StellarWeaverHarborAgent(
            logs_dir=tmp_path / "bad_mode",
            fresh_context_critic=True,
            fresh_context_critic_mode="invalid",
        )

    baseline = StellarWeaverHarborAgent(logs_dir=tmp_path / "base", stream=False)
    critic_agent = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "critic",
        fresh_context_critic="true",
        fresh_context_critic_max_model_calls="10",
        stream=False,
    )
    assert critic_agent.config.harness.fresh_context_critic is True
    assert critic_agent.config.harness.fresh_context_critic_mode == "shadow"
    assert critic_agent.config.harness.fresh_context_critic_max_model_calls == 10
    assert baseline.config.harness.fresh_context_critic is False


async def test_critic_pass_zero_parent_perturbation_and_pre_intervention_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def parent_turn_sequence() -> list[AssistantReply]:
        return [
            AssistantReply(
                "",
                (ToolCall("p1", "bash", json.dumps({"command": "pytest -q"})),),
                "tool_calls",
                usage=_usage(1000, 500, 40),
            ),
            AssistantReply("Parent done", stop_reason="stop", usage=_usage(1400, 900, 30)),
        ]

    base_env = _MockContainerEnv([TerminalExecResult("3 passed\n", "", 0)])
    base_model = FakeModel(parent_turn_sequence())
    base_agent = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "base_logs", provider=base_model, stream=False
    )
    with monkeypatch.context() as fixed:
        ids = count(1)
        fixed.setattr("stellarweaver.session.store.uuid4", lambda ids=ids: UUID(int=next(ids)))
        fixed.setattr("stellarweaver.tools.registry.time", SimpleNamespace(monotonic=lambda: 0.0))
        base_report = await base_agent.run("Implement factor * 2", base_env)

    critic_env = _MockContainerEnv(
        [
            TerminalExecResult("3 passed\n", "", 0),
            TerminalExecResult("EXPECTED_OK 6 == 6\n", "", 0),
        ]
    )

    def critic_model_cb(request: ModelRequest) -> AssistantReply:
        is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system")
        if not is_critic:
            return parent_turn_sequence()[len([r for r in critic_model.requests[:-1] if not any("adversarial completion critic" in m.content for m in r.messages if m.role == "system")])]
        critic_reqs = [
            r for r in critic_model.requests
            if any("adversarial completion critic" in m.content for m in r.messages if m.role == "system")
        ]
        if len(critic_reqs) == 1:
            assert not any("Parent done" in m.content for m in request.messages)
            return AssistantReply(
                "",
                (ToolCall("cb1", "bash", json.dumps({"command": "python3 check.py"})),),
                "tool_calls",
                usage=_usage(800, 0, 50),
            )
        return AssistantReply(
            "",
            (_submit_critique_call("cs1", status="pass", summary="All formulas match"),),
            "tool_calls",
            usage=_usage(950, 750, 40),
        )

    critic_model = FakeModel(critic_model_cb)
    critic_agent = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "critic_logs",
        provider=critic_model,
        fresh_context_critic=True,
        stream=False,
    )
    with monkeypatch.context() as fixed:
        ids = count(1)
        fixed.setattr("stellarweaver.session.store.uuid4", lambda ids=ids: UUID(int=next(ids)))
        fixed.setattr("stellarweaver.tools.registry.time", SimpleNamespace(monotonic=lambda: 0.0))
        critic_report = await critic_agent.run("Implement factor * 2", critic_env)

    parent_reqs = [
        r for r in critic_model.requests
        if not any("adversarial completion critic" in m.content for m in r.messages if m.role == "system")
    ]
    assert len(parent_reqs) == 2
    assert asdict(parent_reqs[0]) == asdict(base_model.requests[0])
    assert asdict(parent_reqs[1]) == asdict(base_model.requests[1])

    assert critic_report["tools_sha256"] == base_report["tools_sha256"]
    assert critic_report["config_sha256"] != base_report["config_sha256"]
    assert critic_report["critic_rounds"] == 1
    assert critic_report["critic_verdicts"] == ["pass"]
    assert critic_report["critic_blocking_findings"] == 0
    assert critic_report["critic_model_calls"] == 2
    assert critic_report["critic_tool_calls"] == 2
    assert critic_report["critic_reported_input_tokens"] == 1750
    assert critic_report["critic_reported_cached_input_tokens"] == 750
    assert critic_report["critic_reported_output_tokens"] == 90
    assert critic_report["critic_run_state"] == "completed"
    assert critic_report["critic_workspace_audit_status"] == "unchanged"
    assert critic_report["critic_workspace_mutated"] is False
    assert critic_report["critic_cleanup_ok"] is True
    assert critic_report["critic_comparison_valid"] is True
    assert len(critic_report["critic_candidate_snapshot_sha256s"]) == 1
    assert critic_report["post_critic_parent_model_calls"] == 0
    assert critic_report["reported_input_tokens"] == 1000 + 1400 + 800 + 950
    assert critic_report["unexpected_prefix_divergences"] == 0
    assert critic_report["critic_prefix_divergences"] == 0
    assert critic_env.cleanup_commands

    manifest = json.loads((tmp_path / "critic_logs" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["fresh_context_critic_schema_version"] == CRITIC_SCHEMA_VERSION
    assert isinstance(manifest["fresh_context_critic_prompt_sha256"], str)
    assert isinstance(manifest["fresh_context_critic_handoff_template_sha256"], str)


async def test_critic_fail_wakes_parent_zero_prefix_divergence_and_shadow_skips_repair(
    tmp_path: Path,
) -> None:
    def make_model_cb(requests_log: list[ModelRequest]):
        def cb(request: ModelRequest) -> AssistantReply:
            requests_log.append(request)
            is_critic = any(
                "adversarial completion critic" in m.content
                for m in request.messages
                if m.role == "system"
            )
            if not is_critic:
                p_reqs = [
                    r for r in requests_log
                    if not any("adversarial completion critic" in m.content for m in r.messages if m.role == "system")
                ]
                if len(p_reqs) == 1:
                    return AssistantReply(
                        "",
                        (ToolCall("p1", "bash", json.dumps({"command": "pytest"})),),
                        "tool_calls",
                        usage=_usage(1000, 0, 20),
                    )
                if len(p_reqs) == 2:
                    return AssistantReply("Initial wrong claim", stop_reason="stop", usage=_usage(1200, 900, 20))
                if len(p_reqs) == 3:
                    assert "verified_receipts" in request.messages[-1].content
                    assert "got 5 expected 10" in request.messages[-1].content
                    return AssistantReply(
                        "",
                        (ToolCall("p2", "bash", json.dumps({"command": "python3 fix_and_test.py"})),),
                        "tool_calls",
                        usage=_usage(1800, 1200, 30),
                    )
                return AssistantReply("Fixed after independent critic", stop_reason="stop", usage=_usage(2100, 1800, 25))

            c_reqs = [
                r for r in requests_log
                if any("adversarial completion critic" in m.content for m in r.messages if m.role == "system")
            ]
            if len(c_reqs) == 1:
                assert "$SW_CRITIC_SCRATCH" in c_reqs[0].messages[0].content
                assert not any("Initial wrong claim" in m.content for m in request.messages)
                return AssistantReply(
                    "",
                    (ToolCall("cb1", "bash", json.dumps({"command": "python3 counterexample.py"})),),
                    "tool_calls",
                    usage=_usage(700, 0, 30),
                )
            return AssistantReply(
                "",
                (
                    _submit_critique_call(
                        "cs1",
                        status="fail",
                        summary="Scaling factor missing 2x",
                        findings=[
                            {
                                "id": "F1",
                                "severity": "blocking",
                                "requirement": "Output must scale by 2x",
                                "expected": "10",
                                "observed_excerpt": "got 5 expected 10",
                                "location": "calc.py:12",
                                "minimal_fix_direction": "Multiply return value by 2",
                                "evidence_call_ids": ["cb1"],
                            }
                        ],
                    ),
                ),
                "tool_calls",
                usage=_usage(900, 700, 35),
            )

        return cb

    # Active mode
    active_env = _MockContainerEnv(
        [
            TerminalExecResult("1 passed\n", "", 0),
            TerminalExecResult("", "AssertionError: got 5 expected 10\n", 1),
            TerminalExecResult("all fixed\n", "", 0),
        ]
    )
    active_reqs: list[ModelRequest] = []
    active_agent = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "active_logs",
        provider=FakeModel(make_model_cb(active_reqs)),
        fresh_context_critic=True,
        fresh_context_critic_mode="active",
        stream=False,
    )
    active_report = await active_agent.run("Scale output by 2", active_env)
    assert active_report["outcome"]["final_text"] == "Fixed after independent critic"
    assert active_report["critic_verdicts"] == ["fail"]
    assert active_report["critic_blocking_findings"] == 1
    assert active_report["post_critic_parent_model_calls"] == 2
    assert active_report["post_critic_parent_tool_calls"] == 1
    assert active_report["unexpected_prefix_divergences"] == 0
    assert "TMPDIR" in active_env.bash_commands[1]["env"]
    assert "PYTHONPYCACHEPREFIX" in active_env.bash_commands[1]["env"]

    # Shadow mode: runs full critic + logs fail verdict, zero extra parent turns
    shadow_env = _MockContainerEnv(
        [
            TerminalExecResult("1 passed\n", "", 0),
            TerminalExecResult("", "AssertionError: got 5 expected 10\n", 1),
        ]
    )
    shadow_reqs: list[ModelRequest] = []
    shadow_agent = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "shadow_logs",
        provider=FakeModel(make_model_cb(shadow_reqs)),
        fresh_context_critic=True,
        fresh_context_critic_mode="shadow",
        stream=False,
    )
    shadow_report = await shadow_agent.run("Scale output by 2", shadow_env)
    assert shadow_report["outcome"]["final_text"] == "Initial wrong claim"
    assert shadow_report["critic_verdicts"] == ["fail"]
    assert shadow_report["post_critic_parent_model_calls"] == 0
    assert shadow_report["post_critic_parent_tool_calls"] == 0


def test_receipt_validation_blocks_zero_bash_unrelated_ls_and_infra_errors() -> None:
    zero_args = SubmitCritiqueArgs(status="pass", summary="Looks good")
    ok, reason, _ = _validate_critique_receipts(zero_args, [])
    assert not ok and "bash" in reason

    ls_receipt = ToolResult(
        call_id="c_ls",
        operation_id="op1",
        tool_name="bash",
        status="ok",
        content="exit_code=0; timed_out=False\nstdout:\ncalc.py\nREADME.md\nstderr:\n",
        data={"command": "ls", "exit_code": 0, "stdout": "calc.py\nREADME.md\n", "stderr": "", "timed_out": False},
    )
    fabricated_fail = SubmitCritiqueArgs.model_validate(
        {
            "status": "fail",
            "summary": "Claimed mismatch",
            "findings": [
                {
                    "id": "F1",
                    "severity": "blocking",
                    "requirement": "Formula",
                    "expected": "10",
                    "observed_excerpt": "got 5 expected 10",
                    "location": "calc.py",
                    "minimal_fix_direction": "Fix",
                    "evidence_call_ids": ["c_ls"],
                }
            ],
        }
    )
    ok_ls, reason_ls, _ = _validate_critique_receipts(fabricated_fail, [ls_receipt])
    assert not ok_ls and "observed_excerpt" in reason_ls

    # Short excerpt without observed_exit_code rejected; synthetic exit_code=0 rejected
    too_short_fail = SubmitCritiqueArgs.model_validate(
        {
            "status": "fail",
            "summary": "Short excerpt",
            "findings": [
                {
                    "id": "F1",
                    "severity": "blocking",
                    "requirement": "Formula",
                    "expected": "10",
                    "observed_excerpt": "NaN",
                    "location": "calc.py",
                    "minimal_fix_direction": "Fix",
                    "evidence_call_ids": ["c_ls"],
                }
            ],
        }
    )
    ok_short, _, _ = _validate_critique_receipts(too_short_fail, [ls_receipt])
    assert not ok_short

    synthetic_header_fail = SubmitCritiqueArgs.model_validate(
        {
            "status": "fail",
            "summary": "Synthetic wrapper",
            "findings": [
                {
                    "id": "F1",
                    "severity": "blocking",
                    "requirement": "Exit code",
                    "expected": "1",
                    "observed_excerpt": "exit_code=0",
                    "location": "calc.py",
                    "minimal_fix_direction": "Fix",
                    "evidence_call_ids": ["c_ls"],
                }
            ],
        }
    )
    ok_syn, _, _ = _validate_critique_receipts(synthetic_header_fail, [ls_receipt])
    assert not ok_syn

    # Empty-stream receipt requires matching structured observed_exit_code (mismatch rejected, match accepted)
    empty_receipt = ToolResult(
        call_id="c_empty",
        operation_id="op_empty",
        tool_name="bash",
        status="error",
        content="exit_code=2; timed_out=False\nstdout:\n\nstderr:\n",
        data={"command": "check_status", "exit_code": 2, "stdout": "", "stderr": "", "timed_out": False},
    )
    mismatch_exit = SubmitCritiqueArgs.model_validate(
        {
            "status": "fail",
            "summary": "Wrong exit code",
            "findings": [
                {
                    "id": "F1",
                    "severity": "blocking",
                    "requirement": "Must exit 0",
                    "expected": "exit 0",
                    "observed_excerpt": "exited non-zero silently",
                    "observed_exit_code": 1,
                    "location": "cli.py",
                    "minimal_fix_direction": "Return 0",
                    "evidence_call_ids": ["c_empty"],
                }
            ],
        }
    )
    ok_mis, _, _ = _validate_critique_receipts(mismatch_exit, [empty_receipt])
    assert not ok_mis
    match_exit = mismatch_exit.model_copy(
        update={
            "findings": [
                mismatch_exit.findings[0].model_copy(update={"observed_exit_code": 2})
            ]
        }
    )
    ok_match, _, _ = _validate_critique_receipts(match_exit, [empty_receipt])
    assert ok_match

    # Cross-receipt split mismatch: exit_code=1 on c_exit and observed_excerpt on c_txt must NOT validate
    r_exit1 = ToolResult(
        call_id="c_exit1",
        operation_id="op_e1",
        tool_name="bash",
        status="error",
        content="exit_code=1",
        data={"command": "fail_silent", "exit_code": 1, "stdout": "unrelated error line\n", "stderr": "", "timed_out": False},
    )
    r_text = ToolResult(
        call_id="c_text",
        operation_id="op_tx",
        tool_name="bash",
        status="ok",
        content="exit_code=0",
        data={"command": "ok_print", "exit_code": 0, "stdout": "AssertionError: got 5 expected 10\n", "stderr": "", "timed_out": False},
    )
    split_args = SubmitCritiqueArgs.model_validate(
        {
            "status": "fail",
            "summary": "Mixed across two commands",
            "findings": [
                {
                    "id": "F1",
                    "severity": "blocking",
                    "requirement": "Formula",
                    "expected": "10",
                    "observed_excerpt": "got 5 expected 10",
                    "observed_exit_code": 1,
                    "location": "calc.py:1",
                    "minimal_fix_direction": "Fix",
                    "evidence_call_ids": ["c_exit1", "c_text"],
                }
            ],
        }
    )
    ok_split, split_reason, _ = _validate_critique_receipts(split_args, [r_exit1, r_text])
    assert not ok_split and "single" in split_reason

    infra_err_receipt = ToolResult(
        call_id="c_err",
        operation_id="op2",
        tool_name="bash",
        status="error",
        content="exec failed",
        data={"code": "TERMINAL_EXEC_ERROR", "exit_code": None, "timed_out": False, "stdout": "", "stderr": "boom"},
    )
    timed_out_receipt = ToolResult(
        call_id="c_to",
        operation_id="op3",
        tool_name="bash",
        status="error",
        content="timed out",
        data={"code": "COMMAND_TIMEOUT", "exit_code": -1, "timed_out": True, "stdout": "", "stderr": "got 5 expected 10"},
    )
    for bad_receipt, cid in ((infra_err_receipt, "c_err"), (timed_out_receipt, "c_to")):
        bad_args = SubmitCritiqueArgs.model_validate(
            {
                "status": "fail",
                "summary": "Bad infra",
                "findings": [
                    {
                        "id": "F1",
                        "severity": "blocking",
                        "requirement": "Formula",
                        "expected": "10",
                        "observed_excerpt": "got 5 expected 10",
                        "location": "calc.py",
                        "minimal_fix_direction": "Fix",
                        "evidence_call_ids": [cid],
                    }
                ],
            }
        )
        ok_bad, _, usable = _validate_critique_receipts(bad_args, [bad_receipt])
        assert not ok_bad and not usable


async def test_submit_critique_tool_gate_and_nonzero_exit_not_stalled_with_finalize(
    tmp_path: Path,
) -> None:
    env = _MockContainerEnv(
        [
            TerminalExecResult("", "counterexample 1 failed\n", 1),
            TerminalExecResult("", "counterexample 2 failed\n", 1),
            TerminalExecResult("", "counterexample 3 failed\n", 1),
            TerminalExecResult("", "counterexample 4 failed\n", 1),
        ]
    )
    seen_finalize_prompts: list[str] = []

    def model_cb(request: ModelRequest) -> AssistantReply:
        if not request.tools:
            # _finalize() call
            seen_finalize_prompts.append(request.messages[-1].content)
            # Return invalid JSON to verify safe downgrade to inconclusive
            return AssistantReply("not valid json verdict", stop_reason="stop", usage=_usage(300, 0, 10))
        is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system")
        if not is_critic:
            return AssistantReply("Parent finished", stop_reason="stop", usage=_usage(500, 0, 20))
        c_turn = sum(1 for r in model.requests[:-1] if any("adversarial completion critic" in m.content for m in r.messages if m.role == "system"))
        if c_turn == 0:
            # First turn tries submit_critique with 0 bash calls -> rejected with valid call_ids hint
            return AssistantReply("", (_submit_critique_call("cs0", status="pass"),), "tool_calls")
        if 1 <= c_turn <= 4:
            return AssistantReply(
                "",
                (ToolCall(f"cb{c_turn}", "bash", json.dumps({"command": f"python3 probe_{c_turn}.py"})),),
                "tool_calls",
            )
        # Keep hitting max_model_calls without submitting to trigger _finalize()
        return AssistantReply("Thinking...", stop_reason="stop")

    model = FakeModel(model_cb)
    cfg = AppConfig(
        limits=LimitsConfig(
            max_model_calls=None, max_tool_calls=100, max_wall_seconds=900,
            max_input_tokens_total=1_000_000, max_output_tokens_total=100_000,
        ),
        harness=terminal_minimal_harness(
            fresh_context_critic=True,
            fresh_context_critic_max_model_calls=6,
            fresh_context_critic_stall_rounds=3,
        ),
    )
    agent = StellarWeaverHarborAgent(logs_dir=tmp_path / "logs", config=cfg, provider=model, stream=False)
    report = await agent.run("Check edge cases", env)

    # 4 non-zero exit_code=1 commands all ran without triggering stalled_rounds=3
    assert len(env.bash_commands) == 4
    assert len(seen_finalize_prompts) == 1
    for i in range(1, 5):
        assert f"[call_id=cb{i}] exit_code=1" in seen_finalize_prompts[0]
        assert f"counterexample {i} failed" in seen_finalize_prompts[0]
    assert report["critic_verdicts"] == ["inconclusive"]
    assert report["post_critic_parent_model_calls"] == 0


async def test_post_handoff_compaction_keeps_original_task_and_critic_report(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(
        model=ModelConfig(context_window_tokens=16_000, max_output_tokens=512),
        context=ContextConfig(recent_complete_rounds=1, fresh_tool_rounds=1),
        limits=LimitsConfig(
            max_model_calls=None, max_tool_calls=100, max_wall_seconds=900,
            max_input_tokens_total=1_000_000, max_output_tokens_total=100_000,
        ),
        harness=terminal_minimal_harness(
            fresh_context_critic=True,
            fresh_context_critic_mode="active",
        ),
    )
    task = "ORIGINAL_TASK_HEADER\n" + ("constraint line\n" * 60) + "ORIGINAL_TASK_FOOTER"
    big_out = "VERBOSE_LOG_LINE " * 1_900 + "\n"
    env = _MockContainerEnv(
        [
            TerminalExecResult(big_out, "", 0),
            TerminalExecResult("", "CRITICAL_BUG_FOUND\n", 1),
            TerminalExecResult(big_out, "", 0),
        ]
    )
    model_reqs: list[ModelRequest] = []

    def cb(request: ModelRequest) -> AssistantReply:
        model_reqs.append(request)
        is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system")
        if is_critic:
            c_n = sum(1 for r in model_reqs[:-1] if any("adversarial completion critic" in m.content for m in r.messages if m.role == "system"))
            if c_n == 0:
                return AssistantReply("", (ToolCall("cb1", "bash", json.dumps({"command": "pytest -k bug"})),), "tool_calls")
            return AssistantReply(
                "",
                (
                    _submit_critique_call(
                        "cs1",
                        status="fail",
                        summary="Found edge bug",
                        findings=[
                            {
                                "id": "F1",
                                "severity": "blocking",
                                "requirement": "Edge invariant",
                                "expected": "0",
                                "observed_excerpt": "CRITICAL_BUG_FOUND",
                                "location": "mod.py:9",
                                "minimal_fix_direction": "Handle empty case",
                                "evidence_call_ids": ["cb1"],
                            }
                        ],
                    ),
                ),
                "tool_calls",
            )
        p_n = sum(1 for r in model_reqs[:-1] if not any("adversarial completion critic" in m.content for m in r.messages if m.role == "system"))
        if p_n == 0:
            return AssistantReply("", (ToolCall("p1", "bash", json.dumps({"command": "run_init"})),), "tool_calls")
        if p_n == 1:
            return AssistantReply("Initial claim", stop_reason="stop", usage=_usage(1000, 0, 20))
        if p_n == 2:
            return AssistantReply("", (ToolCall("p2", "bash", json.dumps({"command": "apply_fix"})),), "tool_calls", usage=_usage(2000, 500, 20))
        return AssistantReply("Final repaired claim", stop_reason="stop", usage=_usage(3000, 1200, 20))

    agent = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "compact_logs",
        config=cfg,
        provider=FakeModel(cb, context_window_tokens=16_000),
        stream=False,
    )
    report = await agent.run(task, env)
    assert report["compactions"] >= 1
    assert report["post_critic_parent_compactions"] >= 1
    assert report["post_compaction_first_request_count"] >= 1
    final_parent_req = [
        r for r in model_reqs if not any("adversarial completion critic" in m.content for m in r.messages if m.role == "system")
    ][-1]
    joined = "\n".join(m.content for m in final_parent_req.messages if m.role == "user")
    assert "ORIGINAL_TASK_HEADER" in joined
    assert "ORIGINAL_TASK_FOOTER" in joined
    assert "CRITICAL_BUG_FOUND" in joined


async def test_budget_reserves_after_retry_and_cancellation_cleanup(
    tmp_path: Path,
) -> None:
    # Scenario A: tight parent model calls (max=10): parent uses 3 calls -> leaves 7 -> minus 4 reserve = 3 max for critic.
    # Critic's 1st request fails transiently (retries internally once), then succeeds within critic's allowance ->
    # parent MUST still keep all 4 reserved calls after Critic stops.
    class _RetryOnceProvider:
        def __init__(self) -> None:
            self.critic_attempts = 0
            self.requests: list[ModelRequest] = []

        def capabilities(self) -> Any:
            return FakeModel().capabilities()

        async def complete(self, request: ModelRequest) -> AssistantReply:
            self.requests.append(request)
            is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system")
            if is_critic:
                self.critic_attempts += 1
                if self.critic_attempts == 1:
                    raise ProviderError("transient 502", retryable=True, status_code=502)
                return AssistantReply(
                    "",
                    (ToolCall(f"cb{self.critic_attempts}", "bash", json.dumps({"command": "echo probe"})),),
                    "tool_calls",
                    usage=_usage(500, 0, 20),
                )
            p_calls = sum(1 for r in self.requests[:-1] if not any("adversarial completion critic" in m.content for m in r.messages if m.role == "system"))
            if p_calls == 0:
                return AssistantReply("", (ToolCall("p1", "bash", json.dumps({"command": "echo hi"})),), "tool_calls", usage=_usage(300, 0, 20))
            return AssistantReply("Parent claim", stop_reason="stop", usage=_usage(400, 200, 20))

        async def stream(self, request: ModelRequest):
            await self.complete(request)
            yield  # pragma: no cover

    cfg_tight = AppConfig(
        limits=LimitsConfig(
            max_model_calls=10,
            max_tool_calls=50,
            max_wall_seconds=900,
            max_input_tokens_total=1_000_000,
            max_output_tokens_total=100_000,
        ),
        harness=terminal_minimal_harness(fresh_context_critic=True, fresh_context_critic_max_model_calls=6),
    )
    env_tight = _MockContainerEnv()
    agent_tight = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "tight_logs", config=cfg_tight, provider=_RetryOnceProvider(), stream=False
    )
    report_tight = await agent_tight.run("Check budget reserve", env_tight)
    # Parent used 2 completed + Critic tried 4 max calls (5 -> 6 -> hit reserve at 10-4=6!) => total <= 6!
    assert report_tight["outcome"]["model_calls"] <= 10 - PARENT_RESERVE_MODEL_CALLS

    # Scenario B: tight wall clock triggers clean skip without running critic
    cfg_wall = AppConfig(
        limits=LimitsConfig(max_model_calls=None, max_tool_calls=50, max_wall_seconds=150),
        harness=terminal_minimal_harness(
            fresh_context_critic=True,
            fresh_context_critic_mode="active",
        ),
    )
    model_wall = FakeModel([AssistantReply("Done", stop_reason="stop", usage=_usage(200, 0, 10))])
    report_wall = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "wall_skip", config=cfg_wall, provider=model_wall, stream=False
    ).run("Short deadline", _MockContainerEnv())
    assert report_wall["critic_rounds"] == 0
    assert report_wall["critic_skipped_reasons"] == ["insufficient_wall_seconds"]

    # Scenario C: enforce_run_budget=False works without parent reserve errors
    ws = tmp_path / "unbounded_ws"
    ws.mkdir()
    unbounded_env = _MockContainerEnv([TerminalExecResult("verified\n", "", 0)])
    with SessionStore(ws, data_dir=tmp_path / "unbounded_state") as store:
        pol = PolicyEngine("benchmark", workspace=ws, allow_terminal_container=True)
        reg = ToolRegistry(ws, pol, builtins=False, command_boundary="harbor-container-exec")
        register_terminal_tools(reg, unbounded_env)

        async def _critic_cb(p: Agent, r: int, f: tuple[dict[str, Any], ...]) -> CriticHandoff:
            return await run_fresh_context_critic(p, unbounded_env, logs_dir=tmp_path / "unbounded_state", round_index=r, prior_findings=f)

        u_agent = Agent(
            FakeModel(
                [
                    AssistantReply("done"),
                    AssistantReply("", (ToolCall("cb1", "bash", json.dumps({"command": "echo x"})),), "tool_calls"),
                    AssistantReply("", (_submit_critique_call("cs1", status="pass"),), "tool_calls"),
                ]
            ),
            reg,
            harness_config=terminal_minimal_harness(fresh_context_critic=True),
            store=store,
            enforce_run_budget=False,
            completion_critic=_critic_cb,
        )
        u_outcome = await u_agent.run("Unbounded check")
        assert u_outcome.run_status == "completed"

    # Scenario D: cancellation propagates out of critic AND runs shielded rm -rf cleanup
    class _CancellingModel(FakeModel):
        async def complete(self, request: ModelRequest) -> AssistantReply:
            if any("adversarial completion critic" in m.content for m in request.messages if m.role == "system"):
                raise asyncio.CancelledError()
            return await super().complete(request)

    cancel_env = _MockContainerEnv()
    ws_c = tmp_path / "cancel_ws"
    ws_c.mkdir()
    with SessionStore(ws_c, data_dir=tmp_path / "cancel_state") as store_c:
        pol_c = PolicyEngine("benchmark", workspace=ws_c, allow_terminal_container=True)
        reg_c = ToolRegistry(ws_c, pol_c, builtins=False, command_boundary="harbor-container-exec")
        register_terminal_tools(reg_c, cancel_env)
        parent_c = Agent(
            _CancellingModel([AssistantReply("Parent done", usage=_usage(100, 0, 10))]),
            reg_c,
            harness_config=terminal_minimal_harness(fresh_context_critic=True),
            store=store_c,
        )
        parent_c._start = time.monotonic()
        parent_c.messages = [Message("system", "sys"), Message("user", "Task")]
        with pytest.raises(asyncio.CancelledError):
            await run_fresh_context_critic(
                parent_c,
                cancel_env,
                logs_dir=tmp_path / "cancel_state",
                round_index=1,
                prior_findings=(),
            )
        events_c = store_c.load_events()
        assert any(e.type == "critic.model_cancelled" for e in events_c)
        assert cancel_env.cleanup_commands


def test_real_probe_ignores_caches_and_detects_workspace_and_site_packages_mutation(
    tmp_path: Path,
) -> None:
    app_dir = tmp_path / "workspace"
    site_dir = tmp_path / "fake_site_packages"
    (app_dir / ".pytest_cache").mkdir(parents=True)
    (app_dir / "__pycache__").mkdir()
    (app_dir / "src").mkdir()
    site_dir.mkdir()
    (app_dir / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (site_dir / "pkg_mod.py").write_text("k = 2\n", encoding="utf-8")

    # Extract Python body from _CONTAINER_PRE_PROBE_SCRIPT and run it in tmp_path.
    # Also verify non-existent usersitepackages directory is skipped without flagging missing_roots.
    nonexistent_user_site = tmp_path / "does_not_exist_user_site"
    py_body = _CONTAINER_PRE_PROBE_SCRIPT.split("<<'__SW_PROBE__'\n", 1)[1].rsplit(
        "\n__SW_PROBE__", 1
    )[0]
    py_body = py_body.replace("site.getsitepackages()", f"[{json.dumps(str(site_dir))}]").replace(
        "site.getusersitepackages()", json.dumps(str(nonexistent_user_site))
    )

    def run_probe() -> tuple[dict[str, Any], dict[str, list[Any]]]:
        proc = subprocess.run(
            [sys.executable, "-c", py_body],
            cwd=app_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        snap_str = proc.stdout.split("__SW_CRITIC_SNAPSHOT_JSON__\n", 1)[1].strip()
        raw_obj = json.loads(snap_str)
        return raw_obj, dict(raw_obj["files"])

    pre_meta, before = run_probe()
    assert pre_meta["missing_roots"] == []
    assert pre_meta["truncated"] is False
    assert pre_meta["read_errors"] == 0

    # Adding .pytest_cache and __pycache__ artifacts must NOT trigger mutation
    (app_dir / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (app_dir / ".pytest_cache" / "v" / "cache" / "lastfailed").write_text("{}", encoding="utf-8")
    (app_dir / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"pyc")
    assert _diff_snapshots(before, run_probe()[1]) == ()

    # Mutating a deliverable or site-packages file MUST be caught
    (site_dir / "pkg_mod.py").write_text("k = 999\n", encoding="utf-8")
    changed = _diff_snapshots(before, run_probe()[1])
    assert any("pkg_mod.py" in path for path in changed)

    # Verify _build_post_probe_script reuses the exact roots and _parse_probe_json flags truncated/read_errors
    post_script = _build_post_probe_script([str(app_dir), str(site_dir)])
    assert str(site_dir).replace("\\", "\\\\") in post_script or str(site_dir) in post_script
    trunc_snap = _parse_probe_json(
        json.dumps({"roots": ["/app"], "files": {}, "truncated": True, "read_errors": 0, "missing_roots": []}),
        return_code=0,
    )
    assert not trunc_snap.complete and trunc_snap.reason == "probe_truncated"
    err_snap = _parse_probe_json(
        json.dumps({"roots": ["/app"], "files": {}, "truncated": False, "read_errors": 2, "missing_roots": []}),
        return_code=0,
    )
    assert not err_snap.complete and err_snap.reason == "probe_read_errors"


async def test_critic_independent_prefix_auditor_isolated_from_parent(
    tmp_path: Path,
) -> None:
    # Emit a simulated parent compaction before Critic runs; verify Critic's own prefix divergence
    # goes to critic.prefix_diverged and does NOT inflate parent unexpected_prefix_divergences or
    # accidentally consume parent's awaiting_post_compaction flag.
    class _DivergingCriticProvider(FakeModel):
        async def complete(self, request: ModelRequest) -> AssistantReply:
            return await super().complete(request)

    env = _MockContainerEnv([TerminalExecResult("ok\n", "", 0)])

    def cb(request: ModelRequest) -> AssistantReply:
        is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system")
        if not is_critic:
            return AssistantReply("Parent done", stop_reason="stop", usage=_usage(500, 100, 10))
        return AssistantReply(
            "",
            (_submit_critique_call("cs1", status="pass"),),
            "tool_calls",
            usage=_usage(400, 0, 10),
        )

    agent = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "iso_logs", provider=_DivergingCriticProvider(cb), fresh_context_critic=True, stream=False
    )
    # Manually exercise ChildRequestScope auditor divergence via direct Agent call
    ws = tmp_path / "iso_ws"
    ws.mkdir()
    with SessionStore(ws, data_dir=tmp_path / "iso_state") as store:
        pol = PolicyEngine("benchmark", workspace=ws, allow_terminal_container=True)
        reg = ToolRegistry(ws, pol, builtins=False, command_boundary="harbor-container-exec")
        register_terminal_tools(reg, env)
        test_agent = Agent(
            FakeModel([AssistantReply("ok1"), AssistantReply("ok2")]),
            reg,
            harness_config=terminal_minimal_harness(fresh_context_critic=True),
            store=store,
        )
        test_agent._start = time.monotonic()
        scope = test_agent._cache_auditor  # reuse PrefixCacheAuditor class instance
        from collections import deque

        from stellarweaver.runtime.loop import ChildRequestScope

        c_scope = ChildRequestScope(
            identity="c_iso",
            deadline=time.monotonic() + 60,
            context_rates=deque(maxlen=6),
            finalizing=False,
            purpose="fresh_critic",
            phase="audit",
            round_index=1,
            cache_auditor=scope,
        )
        await test_agent._request_model(
            ModelRequest((Message("system", "S1"), Message("user", "U1")), ()),
            size := __import__("stellarweaver.context.manager", fromlist=["ContextSize"]).ContextSize(100),
            exploration=c_scope,
        )
        await test_agent._request_model(
            ModelRequest((Message("system", "MUTATED"), Message("user", "U1")), ()),
            size,
            exploration=c_scope,
        )
        ev_types = [e.type for e in store.load_events()]
        assert "critic.prefix_diverged" in ev_types
        assert "cache.prefix_diverged" not in ev_types

    # Verify _summarize_usage_events skips Critic events when attributing post-compaction parent uncached input
    from stellarweaver.domain import AgentEvent

    events = [
        AgentEvent("s", "r", 1, "context.compacted", {}, "t1"),
        AgentEvent(
            "s",
            "r",
            2,
            "model.completed",
            {
                "purpose": "fresh_critic",
                "input_tokens": 500,
                "prompt_cache_hit_tokens": 100,
                "output_tokens": 20,
                "input_estimated": False,
                "output_estimated": False,
            },
            "t2",
        ),
        AgentEvent(
            "s",
            "r",
            3,
            "model.completed",
            {
                "input_tokens": 1200,
                "prompt_cache_hit_tokens": 1000,
                "output_tokens": 30,
                "input_estimated": False,
                "output_estimated": False,
            },
            "t3",
        ),
    ]
    summary = _summarize_usage_events(events, source="events")
    assert summary["post_compaction_first_request_count"] == 1
    assert summary["post_compaction_uncached_input_tokens"] == 200
    _ = agent


async def test_mutated_unverified_and_cleanup_failed_block_active_handoff_and_aggregate(
    tmp_path: Path,
) -> None:
    def make_fail_cb():
        reqs: list[ModelRequest] = []

        def cb(request: ModelRequest) -> AssistantReply:
            reqs.append(request)
            is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system")
            if not is_critic:
                return AssistantReply("Parent initial claim", stop_reason="stop", usage=_usage(500, 100, 10))
            c_n = sum(1 for r in reqs[:-1] if any("adversarial completion critic" in m.content for m in r.messages if m.role == "system"))
            if c_n == 0:
                return AssistantReply("", (ToolCall("cb1", "bash", json.dumps({"command": "pytest -q"})),), "tool_calls", usage=_usage(400, 0, 10))
            return AssistantReply(
                "",
                (
                    _submit_critique_call(
                        "cs1",
                        status="fail",
                        summary="Found defect",
                        findings=[
                            {
                                "id": "F1",
                                "severity": "blocking",
                                "requirement": "Output must match",
                                "expected": "10",
                                "observed_excerpt": "got 5 expected 10",
                                "location": "calc.py:1",
                                "minimal_fix_direction": "Fix formula",
                                "evidence_call_ids": ["cb1"],
                            }
                        ],
                    ),
                ),
                "tool_calls",
                usage=_usage(450, 300, 15),
            )

        return cb

    # 1. mutated workspace blocks active handoff
    mut_env = _MockContainerEnv(
        [TerminalExecResult("", "AssertionError: got 5 expected 10\n", 1)],
        pre_snapshot={"/app/calc.py": [10, 100, "h1"]},
        post_snapshot={"/app/calc.py": [20, 200, "h2"]},
    )
    mut_report = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "mut_logs",
        provider=FakeModel(make_fail_cb()),
        fresh_context_critic=True,
        fresh_context_critic_mode="active",
        stream=False,
    ).run("Task", mut_env)
    assert mut_report["post_critic_parent_model_calls"] == 0
    assert mut_report["critic_workspace_audit_status"] == "mutated"
    assert mut_report["critic_workspace_mutated"] is True
    assert mut_report["critic_comparison_valid"] is False
    assert mut_report["critic_handoff_blocked_reasons"] == ["workspace_mutated"]

    # 2. unverified probe (post probe rc=1) blocks active handoff and keeps mutated=None
    unv_env = _MockContainerEnv(
        [TerminalExecResult("", "AssertionError: got 5 expected 10\n", 1)],
        post_probe_override=TerminalExecResult(stdout="", stderr="permission denied", return_code=1),
    )
    unv_report = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "unv_logs",
        provider=FakeModel(make_fail_cb()),
        fresh_context_critic=True,
        fresh_context_critic_mode="active",
        stream=False,
    ).run("Task", unv_env)
    assert unv_report["post_critic_parent_model_calls"] == 0
    assert unv_report["critic_workspace_audit_status"] == "unverified"
    assert unv_report["critic_workspace_mutated"] is None
    assert unv_report["critic_comparison_valid"] is False
    assert unv_report["critic_handoff_blocked_reasons"] == ["workspace_audit_unverified"]

    # 3. cleanup failure records cleanup_ok=False and blocks active handoff
    cln_env = _MockContainerEnv(
        [TerminalExecResult("", "AssertionError: got 5 expected 10\n", 1)],
        cleanup_exit_code=1,
    )
    cln_report = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "cln_logs",
        provider=FakeModel(make_fail_cb()),
        fresh_context_critic=True,
        fresh_context_critic_mode="active",
        stream=False,
    ).run("Task", cln_env)
    assert cln_report["post_critic_parent_model_calls"] == 0
    assert cln_report["critic_cleanup_ok"] is False
    assert cln_report["critic_comparison_valid"] is False
    assert cln_report["critic_handoff_blocked_reasons"] == ["cleanup_failed"]

    # 4. aggregate_trial_reports preserves planned denominator & separates batch comparison validity
    agg = aggregate_trial_reports(
        [
            {"planned": True, "verdict": "passed", "receipt_status": "present", **mut_report},
            {"planned": True, "verdict": "failed", "receipt_status": "present", **unv_report},
            {"planned": True, "verdict": "failed", "receipt_status": "present", **cln_report},
        ]
    )
    assert agg["tasks"] == 3
    assert agg["passed"] == 1
    assert agg["critic_batch_comparison_valid"] is False
    assert agg["critic_audited_trials"] == 3
    assert agg["critic_audited_clean_trials"] == 0
    assert agg["critic_comparison_invalid_trials"] == 3
    assert agg["critic_mutated_trials"] == 1
    assert agg["critic_unverified_audit_trials"] == 1
    assert agg["critic_cleanup_failed_trials"] == 1

    # Skipped trial alone still yields critic_batch_comparison_valid=False; clean batch yields True
    agg_clean = aggregate_trial_reports(
        [
            {
                "planned": True,
                "lock_status": "valid",
                "verdict": "passed",
                "reward": 1.0,
                "reward_valid": True,
                "harbor_result_present": True,
                "receipt_status": "present",
                "critic_expected": True,
                "critic_run_state": "completed",
                "critic_workspace_audit_status": "unchanged",
                "critic_cleanup_ok": True,
                "critic_audited_cleanly": True,
                "critic_comparison_valid": True,
            }
        ]
    )
    assert agg_clean["critic_batch_comparison_valid"] is True
    assert agg_clean["critic_audited_clean_trials"] == 1

    agg_with_skip = aggregate_trial_reports(
        [
            {
                "planned": True,
                "verdict": "passed",
                "receipt_status": "present",
                "critic_run_state": "completed",
                "critic_workspace_audit_status": "unchanged",
                "critic_cleanup_ok": True,
                "critic_audited_cleanly": True,
                "critic_comparison_valid": True,
            },
            {
                "planned": True,
                "verdict": "passed",
                "receipt_status": "present",
                "critic_run_state": "skipped",
                "critic_workspace_audit_status": "not_run",
                "critic_cleanup_ok": None,
                "critic_audited_cleanly": False,
                "critic_comparison_valid": None,
            },
        ]
    )
    assert agg_with_skip["critic_batch_comparison_valid"] is False
    assert agg_with_skip["critic_skipped_trials"] == 1
    assert agg_with_skip["critic_audited_clean_trials"] == 1


async def test_same_batch_submit_critique_skips_trailing_bash_and_trust_but_verify(
    tmp_path: Path,
) -> None:
    env = _MockContainerEnv([TerminalExecResult("README.md\nsrc/\n", "", 0)])
    reqs: list[ModelRequest] = []

    def cb(request: ModelRequest) -> AssistantReply:
        reqs.append(request)
        is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system")
        if not is_critic:
            return AssistantReply("Parent done", stop_reason="stop", usage=_usage(300, 0, 10))
        c_n = sum(1 for r in reqs[:-1] if any("adversarial completion critic" in m.content for m in r.messages if m.role == "system"))
        if c_n == 0:
            return AssistantReply("", (ToolCall("cb1", "bash", json.dumps({"command": "ls -la"})),), "tool_calls")
        # Return submit_critique AND a trailing bash tool call in the exact same batch
        return AssistantReply(
            "",
            (
                _submit_critique_call("cs1", status="pass", summary="Checked workspace"),
                ToolCall("cb_trailing", "bash", json.dumps({"command": "echo SHOULD_NOT_RUN"})),
            ),
            "tool_calls",
        )

    report = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "batch_skip_logs",
        provider=FakeModel(cb),
        fresh_context_critic=True,
        stream=False,
    ).run("Inspect", env)
    assert report["critic_verdicts"] == ["pass"]
    assert len(env.bash_commands) == 1
    assert all("SHOULD_NOT_RUN" not in c["command"] for c in env.bash_commands)


async def test_retry_caps_input_output_shrink_and_pre_probe_cancel_accounting(
    tmp_path: Path,
) -> None:
    # A. max_model_calls=3: call #1 runs bash (`cb_ok`), call #2 hits retryable 502 -> internal retry
    #    is blocked at gather cap 2 -> _finalize() actually executes as call #3 and sees [call_id=cb_ok]!
    class _RetryBudgetProvider:
        def __init__(self, *, mode: str) -> None:
            self.mode = mode
            self.critic_attempts = 0
            self.finalize_prompts: list[str] = []
            self.critic_requests: list[ModelRequest] = []

        def capabilities(self) -> Any:
            return FakeModel().capabilities()

        async def complete(self, request: ModelRequest) -> AssistantReply:
            is_critic = any("adversarial completion critic" in m.content for m in request.messages if m.role == "system") or not request.tools
            if not is_critic:
                return AssistantReply("Parent finished", stop_reason="stop", usage=_usage(200, 0, 10))
            self.critic_requests.append(request)
            self.critic_attempts += 1
            if not request.tools:
                self.finalize_prompts.append(request.messages[-1].content)
                return AssistantReply("fallback text", stop_reason="stop", usage=_usage(300, 0, 10))
            if self.mode == "calls":
                if self.critic_attempts == 1:
                    return AssistantReply(
                        "",
                        (ToolCall("cb_ok", "bash", json.dumps({"command": "echo probe_obs"})),),
                        "tool_calls",
                        usage=_usage(400, 0, 20),
                    )
                raise ProviderError("transient 502", retryable=True, status_code=502)
            if self.critic_attempts == 1:
                if self.mode == "input":
                    raise ProviderError(
                        "transient after input",
                        retryable=True,
                        status_code=502,
                        usage={"prompt_tokens": 9000, "completion_tokens": 10},
                    )
                if self.mode == "output_stream":
                    raise ProviderError("stream failed", retryable=True, partial_stream=True, partial_output_tokens=1500)
            return AssistantReply(
                "",
                (ToolCall("cb_ok", "bash", json.dumps({"command": "echo ok"})),),
                "tool_calls",
                usage=_usage(400, 0, 20),
            )

        async def stream(self, request: ModelRequest):
            reply = await self.complete(request)
            yield ModelStreamEvent("response.completed", reply=reply)

    cfg_calls3 = AppConfig(
        limits=LimitsConfig(max_model_calls=None, max_tool_calls=50, max_wall_seconds=900),
        harness=terminal_minimal_harness(
            fresh_context_critic=True,
            fresh_context_critic_max_model_calls=3,
        ),
    )
    p_calls3 = _RetryBudgetProvider(mode="calls")
    rep_calls3 = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "calls3_logs",
        config=cfg_calls3,
        provider=p_calls3,
        stream=False,
    ).run("Task", _MockContainerEnv([TerminalExecResult("probe_obs\n", "", 0)]))
    assert len(p_calls3.finalize_prompts) == 1
    assert "[call_id=cb_ok]" in p_calls3.finalize_prompts[0]
    assert rep_calls3["critic_model_calls"] + rep_calls3["critic_failed_model_calls"] == 3

    # B. failed attempt consuming input tokens blocks retry exceeding child max_input_tokens_total
    cfg_input = AppConfig(
        limits=LimitsConfig(max_model_calls=None, max_tool_calls=50, max_wall_seconds=900),
        harness=terminal_minimal_harness(
            fresh_context_critic=True,
            fresh_context_critic_max_input_tokens_total=10_000,
        ),
    )
    p_input = _RetryBudgetProvider(mode="input")
    rep_input = await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "input_cap_logs", config=cfg_input, provider=p_input, stream=False
    ).run("Task", _MockContainerEnv())
    assert p_input.critic_attempts == 1
    assert rep_input["critic_verdicts"] == ["inconclusive"]

    # C. streaming partial output failure shrinks retry request.max_output_tokens cleanly
    cfg_out = AppConfig(
        model=ModelConfig(max_output_tokens=6000, supports_streaming=True),
        limits=LimitsConfig(max_model_calls=None, max_tool_calls=50, max_wall_seconds=900),
        harness=terminal_minimal_harness(
            fresh_context_critic=True,
            fresh_context_critic_max_output_tokens_total=10_000,
        ),
    )
    p_out = _RetryBudgetProvider(mode="output_stream")
    await StellarWeaverHarborAgent(
        logs_dir=tmp_path / "out_cap_logs", config=cfg_out, provider=p_out, stream=True
    ).run("Task", _MockContainerEnv())
    assert len(p_out.critic_requests) >= 2
    assert p_out.critic_requests[1].max_output_tokens < p_out.critic_requests[0].max_output_tokens

    # D. pre-probe cancellation triggers rm -rf cleanup, records critic_run_state='cancelled',
    #    and _summarize_usage_events deduplicates critic.model_cancelled into failed totals.
    cancel_pre_env = _MockContainerEnv(cancel_on_pre_probe=True)
    ws_pre = tmp_path / "pre_cancel_ws"
    ws_pre.mkdir()
    with SessionStore(ws_pre, data_dir=tmp_path / "pre_cancel_state") as store_pre:
        pol_p = PolicyEngine("benchmark", workspace=ws_pre, allow_terminal_container=True)
        reg_p = ToolRegistry(ws_pre, pol_p, builtins=False, command_boundary="harbor-container-exec")
        register_terminal_tools(reg_p, cancel_pre_env)
        parent_p = Agent(
            FakeModel([AssistantReply("Parent done", usage=_usage(100, 0, 10))]),
            reg_p,
            harness_config=terminal_minimal_harness(fresh_context_critic=True),
            store=store_pre,
        )
        parent_p._start = time.monotonic()
        parent_p.messages = [Message("system", "sys"), Message("user", "Task")]
        with pytest.raises(asyncio.CancelledError):
            await run_fresh_context_critic(
                parent_p,
                cancel_pre_env,
                logs_dir=tmp_path / "pre_cancel_state",
                round_index=1,
                prior_findings=(),
            )
        assert cancel_pre_env.cleanup_commands
        ev_pre = store_pre.load_events()
        cancelled_evs = [e for e in ev_pre if e.type == "critic.cancelled"]
        assert len(cancelled_evs) == 1
        assert cancelled_evs[0].payload["comparison_valid"] is False
        assert cancelled_evs[0].payload["cleanup_ok"] is True

    dedup_events = [
        AgentEvent(
            "s",
            "r",
            1,
            "critic.model_cancelled",
            {
                "purpose": "fresh_critic",
                "request_index": 7,
                "input_tokens": 555,
                "output_tokens": 111,
                "input_estimated": True,
                "output_estimated": True,
            },
            "t1",
        ),
        AgentEvent(
            "s",
            "r",
            2,
            "critic.model_cancelled",
            {
                "purpose": "fresh_critic",
                "request_index": 7,
                "input_tokens": 555,
                "output_tokens": 111,
                "input_estimated": True,
                "output_estimated": True,
            },
            "t2",
        ),
    ]
    dedup_sum = _summarize_usage_events(dedup_events, source="events")
    assert dedup_sum["failed_model_calls"] == 1
    assert dedup_sum["failed_estimated_input_tokens"] == 555
    assert dedup_sum["failed_estimated_output_tokens"] == 111
    assert dedup_sum["reported_input_tokens"] == 0


async def test_post_probe_cancel_and_round2_interruption_not_masked_by_round1(
    tmp_path: Path,
) -> None:
    class _PostProbeCancelEnv(_MockContainerEnv):
        async def exec(self, command: str, cwd: str | None = None, env: dict[str, str] | None = None, timeout_sec: int | None = None) -> TerminalExecResult:
            if "__SW_POST__" in command:
                raise asyncio.CancelledError("cancelled during post probe")
            return await super().exec(command, cwd=cwd, env=env, timeout_sec=timeout_sec)

    post_cancel_env = _PostProbeCancelEnv([TerminalExecResult("ok\n", "", 0)])
    ws_pp = tmp_path / "pp_ws"
    ws_pp.mkdir()
    with SessionStore(ws_pp, data_dir=tmp_path / "pp_state") as store_pp:
        pol_pp = PolicyEngine("benchmark", workspace=ws_pp, allow_terminal_container=True)
        reg_pp = ToolRegistry(ws_pp, pol_pp, builtins=False, command_boundary="harbor-container-exec")
        register_terminal_tools(reg_pp, post_cancel_env)
        pp_agent = Agent(
            FakeModel(
                [
                    AssistantReply("", (ToolCall("cb1", "bash", json.dumps({"command": "echo hi"})),), "tool_calls"),
                    AssistantReply("", (_submit_critique_call("cs1", status="pass"),), "tool_calls"),
                ]
            ),
            reg_pp,
            harness_config=terminal_minimal_harness(fresh_context_critic=True),
            store=store_pp,
        )
        pp_agent._start = time.monotonic()
        pp_agent.messages = [Message("system", "sys"), Message("user", "Task")]
        with pytest.raises(asyncio.CancelledError):
            await run_fresh_context_critic(
                pp_agent,
                post_cancel_env,
                logs_dir=tmp_path / "pp_state",
                round_index=1,
                prior_findings=(),
            )
        post_evs = [e for e in store_pp.load_events() if e.type == "critic.cancelled"]
        assert len(post_evs) == 1
        assert post_evs[0].payload.get("phase") == "post_probe"
        assert post_evs[0].payload["comparison_valid"] is False

    # Round 1 completed cleanly, Round 2 interrupted without completion -> report must mark interrupted/unverified!
    ws_r2 = tmp_path / "r2_ws"
    ws_r2.mkdir()
    r2_logs = tmp_path / "r2_logs"
    (r2_logs / "sw").mkdir(parents=True)
    with SessionStore(ws_r2, data_dir=r2_logs / "sw") as r2_store:
        r2_store.append_event("critic.started", {"critic_id": "c1", "critic_round": 1})
        r2_store.append_event(
            "critic.completed",
            {
                "critic_id": "c1",
                "critic_round": 1,
                "status": "fail",
                "workspace_audit_status": "unchanged",
                "cleanup_ok": True,
                "blocking_findings": 1,
            },
        )
        r2_store.append_event("critic.started", {"critic_id": "c2", "critic_round": 2})

        harbor_r2 = StellarWeaverHarborAgent(logs_dir=r2_logs, stream=False)
        pol_r2 = PolicyEngine("benchmark", workspace=ws_r2, allow_terminal_container=True)
        reg_r2 = ToolRegistry(ws_r2, pol_r2, builtins=False, command_boundary="harbor-container-exec")
        dummy_agent = Agent(FakeModel(), reg_r2, store=r2_store)
        rep_r2 = harbor_r2._persist_report_and_context(
            store=r2_store,
            agent=dummy_agent,
            context=None,
            tool_names=["bash"],
            tools_sha256="t",
            preamble_tokens=1,
            config_sha256="c",
            harbor_version="0.25.0",
            started=time.monotonic(),
            outcome_dict=None,
        )
        assert rep_r2["critic_run_state"] == "interrupted"
        assert rep_r2["critic_workspace_audit_status"] == "unverified"
        assert rep_r2["critic_workspace_mutated"] is None
        assert rep_r2["critic_audited_cleanly"] is False
        assert rep_r2["critic_comparison_valid"] is False


def _critic_lock_entry(
    task_name: str,
    digest: str,
    *,
    fresh_context_critic: bool | str | None = None,
    fresh_context_critic_mode: str | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if fresh_context_critic is not None:
        kwargs["fresh_context_critic"] = fresh_context_critic
    if fresh_context_critic_mode is not None:
        kwargs["fresh_context_critic_mode"] = fresh_context_critic_mode
    return {
        "schema_version": 2,
        "task": {"name": task_name, "type": "package", "digest": digest},
        "timeout_multiplier": 1.0,
        "agent": {"name": "stellarweaver", "model_name": "deepseek/flash", "kwargs": kwargs},
        "environment": {"type": "docker", "delete": True},
        "verifier": {"disable": False, "environment_mode": "separate"},
    }


def _write_harbor_critic_trial(
    job_dir: Path,
    trial_name: str,
    *,
    task_name: str,
    reward: float,
    lock_entry: dict[str, Any] | None = None,
    write_agent_report: bool = True,
    critic_run_state: str = "completed",
    critic_enabled: bool = True,
    critic_mode: str | None = "shadow",
) -> None:
    tdir = job_dir / trial_name
    (tdir / "agent").mkdir(parents=True, exist_ok=True)
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
    if not write_agent_report:
        return
    cleanly = critic_run_state == "completed"
    report_payload: dict[str, Any] = {
        "usage_accounting_version": 2,
        "reported_input_tokens": 1000,
        "reported_output_tokens": 100,
        "reported_cached_input_tokens": 800,
        "reported_estimated_cost_usd": 0.001,
        "input_usage_complete": True,
        "output_usage_complete": True,
        "cache_usage_complete": True,
        "critic_enabled": critic_enabled,
        "critic_mode": critic_mode if critic_enabled else None,
        "critic_run_state": critic_run_state,
        "critic_workspace_audit_status": "unchanged" if cleanly else "not_run",
        "critic_workspace_mutated": False if cleanly else None,
        "critic_cleanup_ok": True if cleanly else None,
        "critic_audited_cleanly": cleanly,
        "critic_comparison_valid": True if cleanly else None,
    }
    (tdir / "agent" / "trial-report.json").write_text(
        json.dumps(report_payload), encoding="utf-8"
    )


def test_critic_batch_aggregation_requires_frozen_completeness_and_rejects_not_run_or_missing_receipt() -> None:
    agg_with_not_run = aggregate_trial_reports(
        [
            {
                "planned": True,
                "verdict": "passed",
                "receipt_status": "present",
                "critic_enabled": True,
                "critic_run_state": "completed",
                "critic_workspace_audit_status": "unchanged",
                "critic_cleanup_ok": True,
                "critic_audited_cleanly": True,
                "critic_comparison_valid": True,
            },
            {
                "planned": True,
                "verdict": "passed",
                "receipt_status": "present",
                "critic_enabled": True,
                "critic_run_state": "not_run",
                "critic_workspace_audit_status": "not_run",
                "critic_cleanup_ok": None,
                "critic_audited_cleanly": False,
                "critic_comparison_valid": None,
            },
        ]
    )
    assert agg_with_not_run["tasks"] == 2
    assert agg_with_not_run["passed"] == 2
    assert agg_with_not_run["critic_batch_comparison_valid"] is False
    assert agg_with_not_run["critic_audited_clean_trials"] == 1
    assert agg_with_not_run["critic_not_run_trials"] == 1
    assert agg_with_not_run["critic_missing_agent_report_trials"] == 0

    agg_with_missing_agent_report = aggregate_trial_reports(
        [
            {
                "planned": True,
                "verdict": "passed",
                "receipt_status": "present",
                "agent_report_present": True,
                "critic_enabled": True,
                "critic_run_state": "completed",
                "critic_workspace_audit_status": "unchanged",
                "critic_cleanup_ok": True,
                "critic_audited_cleanly": True,
                "critic_comparison_valid": True,
            },
            {
                "planned": True,
                "verdict": "failed",
                "receipt_status": "partial",
                "agent_report_present": False,
            },
        ]
    )
    assert agg_with_missing_agent_report["tasks"] == 2
    assert agg_with_missing_agent_report["passed"] == 1
    assert agg_with_missing_agent_report["failed"] == 1
    assert agg_with_missing_agent_report["critic_batch_comparison_valid"] is False
    assert agg_with_missing_agent_report["critic_missing_agent_report_trials"] == 1
    assert agg_with_missing_agent_report["critic_not_run_trials"] == 0


def test_summarize_harbor_job_critic_batch_completeness_boundaries(tmp_path: Path) -> None:
    d0 = "sha256:" + ("0" * 64)
    d1 = "sha256:" + ("1" * 64)

    # Case A: 1 clean + 1 not_run under frozen fresh_context_critic=True
    job_a = tmp_path / "job_a_not_run"
    job_a.mkdir()
    lock_a = [
        _critic_lock_entry("terminal-bench/t0", d0, fresh_context_critic=True),
        _critic_lock_entry("terminal-bench/t1", d1, fresh_context_critic=True),
    ]
    (job_a / "lock.json").write_text(json.dumps({"trials": lock_a}), encoding="utf-8")
    _write_harbor_critic_trial(
        job_a, "t0__1", task_name="terminal-bench/t0", reward=1.0, lock_entry=lock_a[0], critic_run_state="completed"
    )
    _write_harbor_critic_trial(
        job_a, "t1__2", task_name="terminal-bench/t1", reward=1.0, lock_entry=lock_a[1], critic_run_state="not_run"
    )
    sum_a = summarize_harbor_job(job_a)
    assert sum_a["batch_complete"] is True
    assert sum_a["summary"]["tasks"] == 2
    assert sum_a["summary"]["passed"] == 2
    assert sum_a["summary"]["critic_expected_trials"] == 2
    assert sum_a["summary"]["critic_audited_trials"] == 1
    assert sum_a["summary"]["critic_audited_clean_trials"] == 1
    assert sum_a["summary"]["critic_not_run_trials"] == 1
    assert sum_a["summary"]["critic_missing_agent_report_trials"] == 0
    assert sum_a["summary"]["critic_batch_comparison_valid"] is False
    assert "not_run=1" in sum_a["markdown"]
    assert "missing_agent_report=0" in sum_a["markdown"]

    # Case B: 1 clean + 1 missing agent/trial-report.json (result.json only)
    job_b = tmp_path / "job_b_missing_agent_report"
    job_b.mkdir()
    (job_b / "lock.json").write_text(json.dumps({"trials": lock_a}), encoding="utf-8")
    _write_harbor_critic_trial(
        job_b, "t0__1", task_name="terminal-bench/t0", reward=1.0, lock_entry=lock_a[0], critic_run_state="completed"
    )
    _write_harbor_critic_trial(
        job_b, "t1__2", task_name="terminal-bench/t1", reward=0.0, lock_entry=lock_a[1], write_agent_report=False
    )
    sum_b = summarize_harbor_job(job_b)
    assert sum_b["summary"]["tasks"] == 2
    assert sum_b["summary"]["passed"] == 1
    assert sum_b["summary"]["failed"] == 1
    assert sum_b["summary"]["critic_missing_agent_report_trials"] == 1
    assert sum_b["summary"]["critic_not_run_trials"] == 0
    assert sum_b["summary"]["critic_batch_comparison_valid"] is False

    # Case C: both not_run under frozen fresh_context_critic=True -> False (never None)
    job_c = tmp_path / "job_c_all_not_run"
    job_c.mkdir()
    (job_c / "lock.json").write_text(json.dumps({"trials": lock_a}), encoding="utf-8")
    _write_harbor_critic_trial(
        job_c, "t0__1", task_name="terminal-bench/t0", reward=0.0, lock_entry=lock_a[0], critic_run_state="not_run"
    )
    _write_harbor_critic_trial(
        job_c, "t1__2", task_name="terminal-bench/t1", reward=0.0, lock_entry=lock_a[1], critic_run_state="not_run"
    )
    sum_c = summarize_harbor_job(job_c)
    assert sum_c["summary"]["critic_expected_trials"] == 2
    assert sum_c["summary"]["critic_not_run_trials"] == 2
    assert sum_c["summary"]["critic_batch_comparison_valid"] is False

    # Case D: both cleanly audited under frozen fresh_context_critic=True -> True
    job_d = tmp_path / "job_d_all_clean"
    job_d.mkdir()
    (job_d / "lock.json").write_text(json.dumps({"trials": lock_a}), encoding="utf-8")
    _write_harbor_critic_trial(
        job_d, "t0__1", task_name="terminal-bench/t0", reward=1.0, lock_entry=lock_a[0], critic_run_state="completed"
    )
    _write_harbor_critic_trial(
        job_d, "t1__2", task_name="terminal-bench/t1", reward=1.0, lock_entry=lock_a[1], critic_run_state="completed"
    )
    sum_d = summarize_harbor_job(job_d)
    assert sum_d["summary"]["critic_audited_clean_trials"] == 2
    assert sum_d["summary"]["critic_not_run_trials"] == 0
    assert sum_d["summary"]["critic_missing_agent_report_trials"] == 0
    assert sum_d["summary"]["critic_batch_comparison_valid"] is True

    # Case E: non-Critic frozen lock.json -> critic_batch_comparison_valid is None and counts stay 0
    job_e = tmp_path / "job_e_non_critic"
    job_e.mkdir()
    lock_e = [
        _critic_lock_entry("terminal-bench/t0", d0, fresh_context_critic=None),
        _critic_lock_entry("terminal-bench/t1", d1, fresh_context_critic=False),
    ]
    (job_e / "lock.json").write_text(json.dumps({"trials": lock_e}), encoding="utf-8")
    _write_harbor_critic_trial(
        job_e,
        "t0__1",
        task_name="terminal-bench/t0",
        reward=1.0,
        lock_entry=lock_e[0],
        critic_run_state="not_run",
        critic_enabled=False,
    )
    _write_harbor_critic_trial(
        job_e,
        "t1__2",
        task_name="terminal-bench/t1",
        reward=1.0,
        lock_entry=lock_e[1],
        critic_run_state="not_run",
        critic_enabled=False,
    )
    sum_e = summarize_harbor_job(job_e)
    assert sum_e["summary"]["critic_batch_comparison_valid"] is None
    assert sum_e["summary"]["critic_expected_trials"] == 0
    assert sum_e["summary"]["critic_not_run_trials"] == 0
    assert sum_e["summary"]["critic_missing_agent_report_trials"] == 0
    assert "Critic Batch Comparison" not in render_markdown_summary(sum_e["trials"], agg=sum_e["summary"])

    # Case F: lock.json absent with 1 clean Critic trial on disk -> unverifiable denominator blocks batch validity
    job_f = tmp_path / "job_f_no_lock"
    job_f.mkdir()
    _write_harbor_critic_trial(
        job_f, "t0__1", task_name="terminal-bench/t0", reward=1.0, critic_run_state="completed"
    )
    sum_f = summarize_harbor_job(job_f)
    assert sum_f["planned_denominator_verifiable"] is False
    assert sum_f["summary"]["critic_batch_comparison_valid"] is False


@pytest.mark.parametrize("verifier_result", ["missing", "corrupt", "unscored", "valid_failure"])
def test_critic_batch_requires_usable_official_results(
    tmp_path: Path, verifier_result: str
) -> None:
    plan = [
        _critic_lock_entry(f"terminal-bench/t{idx}", f"sha256:{str(idx) * 64}", fresh_context_critic=True)
        for idx in range(2)
    ]
    (tmp_path / "lock.json").write_text(json.dumps({"trials": plan}), encoding="utf-8")
    _write_harbor_critic_trial(
        tmp_path, "t0__1", task_name="terminal-bench/t0", reward=1.0, lock_entry=plan[0]
    )
    _write_harbor_critic_trial(
        tmp_path, "t1__2", task_name="terminal-bench/t1", reward=0.0, lock_entry=plan[1]
    )
    result = tmp_path / "t1__2" / "result.json"
    if verifier_result == "missing":
        result.unlink()
    elif verifier_result == "corrupt":
        result.write_text("{", encoding="utf-8")
    elif verifier_result == "unscored":
        result.write_text(json.dumps({"task_name": "terminal-bench/t1"}), encoding="utf-8")

    job = summarize_harbor_job(tmp_path)
    summary = job["summary"]
    scored = verifier_result == "valid_failure"
    assert summary["critic_batch_comparison_valid"] is scored
    assert summary["critic_audited_clean_trials"] == 2
    assert summary["critic_missing_verifier_result_trials"] == int(verifier_result == "missing")
    assert summary["critic_invalid_verifier_result_trials"] == int(
        verifier_result in {"corrupt", "unscored"}
    )
    assert summary["tasks"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == int(scored)
    assert summary["unscored"] == int(not scored)
    assert summary["resolved_rate"] == 0.5
    if not scored:
        assert "Critic Comparison Blockers" in job["markdown"]


@pytest.mark.parametrize(
    ("planned_enabled", "planned_mode", "actual_enabled", "actual_mode", "manifest_mode", "status"),
    [
        (False, None, True, "shadow", "shadow", "mismatch"),
        (True, "active", False, None, None, "mismatch"),
        ("true", "active", True, "shadow", "shadow", "mismatch"),
        (True, "shadow", True, "shadow", "active", "mismatch"),
        (True, None, True, None, None, "unverified"),
        (True, None, True, None, "shadow", "matched"),
    ],
)
def test_critic_batch_compares_runtime_config_with_frozen_settings(
    tmp_path: Path,
    planned_enabled: bool | str,
    planned_mode: str | None,
    actual_enabled: bool,
    actual_mode: str | None,
    manifest_mode: str | None,
    status: str,
) -> None:
    lock_entry = _critic_lock_entry(
        "terminal-bench/t0",
        "sha256:" + ("0" * 64),
        fresh_context_critic=planned_enabled,
        fresh_context_critic_mode=planned_mode,
    )
    (tmp_path / "lock.json").write_text(json.dumps({"trials": [lock_entry]}), encoding="utf-8")
    _write_harbor_critic_trial(
        tmp_path,
        "t0__1",
        task_name="terminal-bench/t0",
        reward=1.0,
        lock_entry=lock_entry,
        critic_run_state="completed" if actual_enabled else "not_run",
        critic_enabled=actual_enabled,
        critic_mode=actual_mode,
    )
    (tmp_path / "t0__1" / "agent" / "manifest.json").write_text(
        json.dumps(
            {
                "harness": {
                    "fresh_context_critic": actual_enabled,
                    "fresh_context_critic_mode": manifest_mode,
                }
            }
        ),
        encoding="utf-8",
    )

    job = summarize_harbor_job(tmp_path)
    trial = job["trials"][0]
    assert trial["critic_expected"] is bool(planned_enabled)
    assert trial["critic_expected_mode"] == ((planned_mode or "shadow") if planned_enabled else None)
    assert trial["critic_enabled"] is actual_enabled
    assert trial["critic_mode"] == (actual_mode or manifest_mode)
    assert trial["critic_config_mismatch"] is (status == "mismatch")
    assert trial["critic_config_unverified"] is (status == "unverified")
    assert job["summary"]["critic_expected_trials"] == int(bool(planned_enabled))
    assert job["summary"]["critic_config_mismatch_trials"] == int(status == "mismatch")
    assert job["summary"]["critic_config_unverified_trials"] == int(status == "unverified")
    assert job["summary"]["critic_batch_comparison_valid"] is (status == "matched")
    assert job["summary"]["passed"] == 1


def test_summarizing_single_critic_trial_does_not_verify_batch(tmp_path: Path) -> None:
    lock_entry = _critic_lock_entry(
        "terminal-bench/t0", "sha256:" + ("0" * 64), fresh_context_critic=True
    )
    (tmp_path / "lock.json").write_text(json.dumps({"trials": [lock_entry]}), encoding="utf-8")
    _write_harbor_critic_trial(
        tmp_path, "t0__1", task_name="terminal-bench/t0", reward=1.0, lock_entry=lock_entry
    )

    trial = summarize_harbor_job(tmp_path / "t0__1")
    assert trial["planned_denominator_verifiable"] is False
    assert trial["summary"]["tasks"] is None
    assert trial["summary"]["passed"] == 1
    assert trial["summary"]["resolved_rate"] is None
    assert trial["summary"]["critic_batch_comparison_valid"] is False
    assert "use the Harbor job directory" in trial["markdown"]
    assert summarize_harbor_job(tmp_path)["summary"]["critic_batch_comparison_valid"] is True
