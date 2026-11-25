"""Unit tests for Harness Phase 3a (Tasks 3.1 - 3.3: non-interactive env, terminal-minimal, Harbor)."""

from __future__ import annotations

import inspect
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from stellarweaver.config import (
    AppConfig,
    ExecutionConfig,
    HarnessConfig,
    ModelConfig,
    PolicyConfig,
    RetryConfig,
    long_horizon_deepseek_flash,
    swe_python_harness,
    terminal_minimal_harness,
)
from stellarweaver.domain import (
    AgentEvent,
    AssistantReply,
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
    ToolCall,
)
from stellarweaver.evals.terminal_bench import (
    HarborAgentContext,
    StellarWeaverHarborAgent,
    aggregate_trial_reports,
    build_terminal_agent,
    render_markdown_summary,
)
from stellarweaver.policy import PolicyEngine
from stellarweaver.policy.risk import assess
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import (
    NONINTERACTIVE_ENV,
    controlled_environment,
    execute_command,
)
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.terminal import TerminalExecResult, register_terminal_tools

# ---------------------------------------------------------------------------
# Task 3.1: Host non-interactive env injection, timeout bound, cleanup intact
# ---------------------------------------------------------------------------


def test_controlled_environment_injects_noninteractive_defaults_and_allows_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STELLARWEAVER_API_KEY", "sk-secret-must-not-leak")
    env = controlled_environment()
    for key, expected in NONINTERACTIVE_ENV.items():
        assert env[key] == expected
    assert "STELLARWEAVER_API_KEY" not in env

    overridden = controlled_environment({"CI": "false", "DEBIAN_FRONTEND": "readline"})
    assert overridden["CI"] == "false"
    assert overridden["DEBIAN_FRONTEND"] == "readline"
    assert overridden["GIT_TERMINAL_PROMPT"] == "0"
    assert overridden["PIP_NO_INPUT"] == "1"


async def test_execution_config_max_timeout_relaxed_to_1800_and_host_cleanup_intact(
    tmp_path: Path,
) -> None:
    cfg = ExecutionConfig(default_timeout_seconds=120, max_timeout_seconds=1800)
    assert cfg.default_timeout_seconds == 120
    assert cfg.max_timeout_seconds == 1800
    with pytest.raises(ValidationError):
        ExecutionConfig(max_timeout_seconds=1801)

    # Host execute_command must NEVER accept keep_background or weaken process cleanup.
    sig = inspect.signature(execute_command)
    assert "keep_background" not in sig.parameters

    res = await execute_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['DEBIAN_FRONTEND'], os.environ['GIT_TERMINAL_PROMPT'], "
            "os.environ['PIP_NO_INPUT'], os.environ['CI'])",
        ],
        tmp_path,
        config=cfg,
        timeout_seconds=600,
    )
    assert res["status"] == "ok", res
    assert res["data"]["stdout"].strip() == "noninteractive 0 1 true"
    assert res["data"]["process_management"]["cleanup_confirmed"] is True


# ---------------------------------------------------------------------------
# Task 3.2: Named profiles (swe-python vs terminal-minimal) & single bash MVP
# ---------------------------------------------------------------------------


def test_named_profiles_swe_python_vs_terminal_minimal() -> None:
    swe = swe_python_harness()
    assert swe.profile == "swe-python"
    assert swe.development_tools is True
    assert swe.issue_navigation is True
    assert swe.import_aware_repomap is True
    assert swe.include_linter is False
    assert swe.sticky_context_checkpoint is True

    term = terminal_minimal_harness()
    assert term.profile == "terminal-minimal"
    assert term.development_tools is False
    assert term.issue_navigation is False
    assert term.import_aware_repomap is False
    assert term.subagent_exploration is False
    assert term.post_edit_syntax_hook is False
    assert term.patch_staging is False
    assert term.multi_file_apply is False
    assert term.include_linter is False
    assert term.sticky_context_checkpoint is True
    assert term.calibrated_context_budget is True
    assert term.repair_controller == "feedback"
    assert term.max_length_continuations == 3
    assert term.repeated_failure_hard_threshold == 6
    assert term.stream_repetition_detection is True

    preset_term = long_horizon_deepseek_flash(harness_profile="terminal-minimal")
    assert preset_term.harness.profile == "terminal-minimal"
    assert preset_term.execution.default_timeout_seconds == 120
    assert preset_term.execution.max_timeout_seconds == 1800
    assert preset_term.model.reasoning_effort == "max"
    assert preset_term.model.max_output_tokens == 65_536

    preset_swe = long_horizon_deepseek_flash()
    assert preset_swe.harness.profile == "swe-python"
    assert preset_swe.execution.max_timeout_seconds == 300


@dataclass
class _MockExecResult:
    stdout: str | None = None
    stderr: str | None = None
    return_code: int = 0


class _MockHarborEnvironment:
    """Offline mock mirroring Harbor BaseEnvironment.exec(command, cwd, env, timeout_sec)."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> Any:
        record = {"command": command, "cwd": cwd, "env": env, "timeout_sec": timeout_sec}
        self.calls.append(record)
        return self.handler(record)


async def test_terminal_minimal_exposes_only_bash_and_disables_host_components(
    tmp_path: Path,
) -> None:
    # Populate host workspace with AGENTS.md and a host Python file that MUST NOT leak into context.
    (tmp_path / "AGENTS.md").write_text("SECRET_HOST_AGENTS_DIRECTIVE\n", encoding="utf-8")
    (tmp_path / "host_only_module.py").write_text("def host_secret_fn(): pass\n", encoding="utf-8")

    env = _MockHarborEnvironment(lambda rec: _MockExecResult(stdout="ok\n", stderr="", return_code=0))
    seen_requests: list[ModelRequest] = []
    events: list[AgentEvent] = []

    def respond(req: ModelRequest) -> AssistantReply:
        seen_requests.append(req)
        if len(seen_requests) == 1:
            return AssistantReply(
                text="",
                tool_calls=(ToolCall("b1", "bash", json.dumps({"command": "echo hello"})),),
                stop_reason="tool_calls",
            )
        return AssistantReply(text="Done inside container.", stop_reason="stop")

    config = long_horizon_deepseek_flash(harness_profile="terminal-minimal")
    with SessionStore(tmp_path) as store:
        agent = build_terminal_agent(config, FakeModel(respond), store, env, on_event=events.append)
        tool_names = [schema["function"]["name"] for schema in agent.registry.schemas()]
        assert tool_names == ["bash"]
        assert agent.project_context is False
        assert agent.capture_host_changes is False
        outcome = await agent.run("Check container")

    assert outcome.run_status == "completed"
    assert outcome.final_text == "Done inside container."
    assert len(env.calls) == 1
    assert env.calls[0]["command"] == "echo hello"
    assert env.calls[0]["env"] == NONINTERACTIVE_ENV

    # Host ChangesManager, InstructionLoader, and RepoMap must never execute or leak into prompts.
    event_types = {e.type for e in events}
    assert "changes.baseline" not in event_types
    assert "instructions.loaded" not in event_types
    for req in seen_requests:
        joined = "\n".join(m.content for m in req.messages)
        assert "SECRET_HOST_AGENTS_DIRECTIVE" not in joined
        assert "host_secret_fn" not in joined
        assert [t["function"]["name"] for t in req.tools] == ["bash"]
    # Prefix stability: system + user messages at the head of turn 2 match turn 1 byte-for-byte.
    assert seen_requests[1].messages[:2] == seen_requests[0].messages[:2]


def test_terminal_bash_cannot_be_registered_or_exposed_outside_benchmark_container(
    tmp_path: Path,
) -> None:
    env = _MockHarborEnvironment(lambda _rec: TerminalExecResult())

    # 1. Interactive/non-benchmark presets MUST reject terminal bash registration.
    for preset in ("ask", "auto", "read-only", "workspace-edit"):
        engine = PolicyEngine(preset, workspace=tmp_path, allow_terminal_container=True)
        assert engine.allow_terminal_container is False
        registry = ToolRegistry(tmp_path, engine, builtins=False, command_boundary="b")
        with pytest.raises(ValueError, match="allow_terminal_container"):
            register_terminal_tools(registry, env)

    # 2. Benchmark without allow_terminal_container=True MUST reject registration.
    bench_no_term = PolicyEngine("benchmark", workspace=tmp_path)
    reg_no_term = ToolRegistry(tmp_path, bench_no_term, builtins=False, command_boundary="b")
    with pytest.raises(ValueError, match="allow_terminal_container"):
        register_terminal_tools(reg_no_term, env)

    # 3. Missing command_boundary MUST reject registration.
    bench_term = PolicyEngine("benchmark", workspace=tmp_path, allow_terminal_container=True)
    reg_no_bound = ToolRegistry(tmp_path, bench_term, builtins=False)
    with pytest.raises(ValueError, match="command_boundary"):
        register_terminal_tools(reg_no_bound, env)

    # 4. Capability boundary denies unisolated host bash execution.
    risk_host = assess({"tool": "bash", "executor": "host"})
    assert risk_host.level == "L3"
    assert risk_host.recommendation == "deny"
    risk_sandbox = assess({"tool": "bash", "executor": "sandbox"})
    assert risk_sandbox.level == "L1"
    assert risk_sandbox.recommendation == "ask"

    # 5. Standard CLI/Web/swe-python build_agent never exposes `bash`.
    for preset in ("ask", "auto", "read-only", "benchmark"):
        cfg = AppConfig(policy=PolicyConfig(preset=preset), harness=HarnessConfig())
        ws = tmp_path / f"store_{preset}"
        ws.mkdir()
        with SessionStore(ws) as store:
            agent = build_agent(cfg, FakeModel([]), store)
            names = {schema["function"]["name"] for schema in agent.registry.schemas()}
            assert "bash" not in names


# ---------------------------------------------------------------------------
# Task 3.3: Harbor native adapter (multi-turn bash, errors, timeouts, metrics)
# ---------------------------------------------------------------------------


async def test_harbor_adapter_multiturn_bash_nonzero_exit_and_timeout(tmp_path: Path) -> None:
    def handle(rec: dict[str, Any]) -> _MockExecResult:
        cmd = rec["command"]
        if cmd == "ls /app":
            return _MockExecResult(stdout="main.c\nMakefile\n", stderr="", return_code=0)
        if cmd == "make test":
            return _MockExecResult(stdout="", stderr="error: undefined symbol\n", return_code=2)
        if cmd == "sleep 999":
            raise TimeoutError("exec timed out")
        if cmd == "make && ./run":
            assert rec["cwd"] == "/app"
            assert rec["timeout_sec"] == 600
            assert rec["env"] == NONINTERACTIVE_ENV
            return _MockExecResult(stdout="ALL PASSED\n", stderr="", return_code=0)
        raise AssertionError(f"Unexpected command: {cmd}")

    env = _MockHarborEnvironment(handle)
    model = FakeModel(
        [
            AssistantReply(
                text="",
                tool_calls=(ToolCall("c1", "bash", json.dumps({"command": "ls /app"})),),
                stop_reason="tool_calls",
                usage={
                    "prompt_tokens": 1000,
                    "completion_tokens": 50,
                    "total_tokens": 1050,
                    "prompt_cache_hit_tokens": 200,
                },
            ),
            AssistantReply(
                text="",
                tool_calls=(ToolCall("c2", "bash", json.dumps({"command": "make test"})),),
                stop_reason="tool_calls",
                usage={
                    "prompt_tokens": 1500,
                    "completion_tokens": 60,
                    "total_tokens": 1560,
                    "prompt_cache_hit_tokens": 1000,
                },
            ),
            AssistantReply(
                text="",
                tool_calls=(
                    ToolCall(
                        "c3",
                        "bash",
                        json.dumps({"command": "sleep 999", "timeout_seconds": 5}),
                    ),
                ),
                stop_reason="tool_calls",
                usage={
                    "prompt_tokens": 2000,
                    "completion_tokens": 40,
                    "total_tokens": 2040,
                    "prompt_cache_hit_tokens": 1500,
                },
            ),
            AssistantReply(
                text="",
                tool_calls=(
                    ToolCall(
                        "c4",
                        "bash",
                        json.dumps(
                            {"command": "make && ./run", "cwd": "/app", "timeout_seconds": 600}
                        ),
                    ),
                ),
                stop_reason="tool_calls",
                usage={
                    "prompt_tokens": 2500,
                    "completion_tokens": 40,
                    "total_tokens": 2540,
                    "prompt_cache_hit_tokens": 2000,
                },
            ),
            AssistantReply(
                text="Build and verification passed.",
                stop_reason="stop",
                usage={
                    "prompt_tokens": 3000,
                    "completion_tokens": 30,
                    "total_tokens": 3030,
                    "prompt_cache_hit_tokens": 2500,
                },
            ),
        ]
    )

    logs_dir = tmp_path / "trial_logs"
    adapter = StellarWeaverHarborAgent(
        logs_dir=logs_dir,
        model_name="deepseek/deepseek-flash",
        provider=model,
        stream=False,
    )
    assert adapter.name() == "stellarweaver"
    assert adapter.version()
    assert adapter.config.model.model == "deepseek-flash"
    assert adapter.config.model.reasoning_effort == "max"
    assert adapter.config.model.max_output_tokens == 65_536
    assert adapter.config.harness.profile == "terminal-minimal"
    assert adapter.config.execution.max_timeout_seconds == 1800

    await adapter.setup(env)
    ctx = HarborAgentContext()
    report = await adapter.run("Build and fix /app", env, ctx)

    assert report["outcome"]["run_status"] == "completed"
    assert report["outcome"]["final_text"] == "Build and verification passed."
    assert report["profile"] == "terminal-minimal"
    assert report["tools"] == ["bash"]
    assert report["preamble_tokens_estimated"] > 0
    assert report["completed_model_calls"] == 5
    assert report["failed_model_calls"] == 0
    assert report["cached_input_tokens"] == 7200
    assert report["outcome"]["input_tokens"] == 10000
    assert report["outcome"]["output_tokens"] == 220
    assert report["cache_hit_ratio"] == 0.72

    # Check tool results recorded for nonzero exit and timeout.
    invoked = report["tools_invoked"]
    assert invoked[0]["status"] == "ok"
    assert invoked[1]["status"] == "error"
    assert invoked[1]["data"]["exit_code"] == 2
    assert invoked[1]["data"]["code"] == "COMMAND_FAILED"
    assert invoked[2]["status"] == "error"
    assert invoked[2]["data"]["timed_out"] is True
    assert invoked[2]["data"]["code"] == "COMMAND_TIMEOUT"
    assert invoked[3]["status"] == "ok"
    assert "ALL PASSED" in invoked[3]["data"]["stdout"]

    # Harbor AgentContext telemetry populated.
    assert ctx.n_input_tokens == 10000
    assert ctx.n_cache_tokens == 7200
    assert ctx.n_output_tokens == 220
    assert ctx.cost_usd is not None and ctx.cost_usd > 0
    assert ctx.metadata["profile"] == "terminal-minimal"
    assert ctx.metadata["cached_input_tokens"] == 7200

    # Manifest and trial report persisted on disk.
    manifest = json.loads((logs_dir / "manifest.json").read_text(encoding="utf-8"))
    saved_report = json.loads((logs_dir / "trial-report.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == "terminal-minimal"
    assert manifest["tools"] == ["bash"]
    assert saved_report["cached_input_tokens"] == 7200


class _FlakyOnceProvider:
    """Fails once with retryable ProviderError before returning cached completions."""

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(tool_calling=True, streaming=False, context_window_tokens=512_000)

    async def complete(self, request: ModelRequest) -> AssistantReply:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("transient 429", retryable=True, status_code=429)
        return AssistantReply(
            text="Recovered cleanly.",
            stop_reason="stop",
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "total_tokens": 1020,
                "prompt_cache_hit_tokens": 850,
            },
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        raise AssertionError("Non-streaming test")
        yield  # pragma: no cover


async def test_harbor_adapter_cache_accounting_ignores_failed_retry_and_aggregates_summary(
    tmp_path: Path,
) -> None:
    env = _MockHarborEnvironment(lambda _rec: _MockExecResult(stdout="", stderr="", return_code=0))
    cfg = AppConfig(
        model=ModelConfig(supports_streaming=False, context_window_tokens=512_000),
        policy=PolicyConfig(preset="benchmark"),
        retry=RetryConfig(max_retries=3, base_delay_seconds=0.0, max_delay_seconds=1.0),
        harness=terminal_minimal_harness(),
    )
    adapter = StellarWeaverHarborAgent(
        logs_dir=tmp_path / "retry_trial",
        config=cfg,
        provider=_FlakyOnceProvider(),
        stream=False,
    )
    report = await adapter.run("Quick task", env, HarborAgentContext())
    assert report["outcome"]["run_status"] == "completed"
    assert report["failed_model_calls"] == 1
    assert report["completed_model_calls"] == 1
    # Must NOT become None because of the transient retry failure!
    assert report["cached_input_tokens"] == 850

    summary = aggregate_trial_reports([{**report, "resolved": True}])
    assert summary["tasks"] == 1
    assert summary["resolved"] == 1
    assert summary["resolved_rate"] == 1.0
    assert summary["total_cached_input_tokens"] == 850

    markdown = render_markdown_summary([{**report, "resolved": True}])
    assert "| Resolved | 1 (100.0%) |" in markdown
    assert "| Cached Input Tokens | 850 |" in markdown
