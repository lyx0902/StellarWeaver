"""Harbor native Agent adapter for Terminal-Bench 4.1 (Phase 3a single-bash MVP).

Delegates container execution and lifecycle strictly to Harbor's BaseEnvironment.exec().
Does NOT orchestrate `docker run` containers or implement task verifiers.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from stellarweaver.cli import build_provider
from stellarweaver.config import AppConfig, long_horizon_deepseek_flash, terminal_minimal_harness
from stellarweaver.context import estimate_tokens
from stellarweaver.domain import JSONDict
from stellarweaver.evals.provenance import canonical_json
from stellarweaver.evals.swebench import save
from stellarweaver.evals.swebench_pilot import digest
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ModelProvider
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.loop import Agent, DeltaSink, EventSink
from stellarweaver.session.journal import RunJournal
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.terminal import TerminalExecutor, register_terminal_tools


class _FallbackHarborBaseAgent:
    """Lightweight offline fallback matching Harbor's BaseAgent constructor contract."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.logs_dir = Path(logs_dir)
        self.model_name = model_name
        self.extra_kwargs = kwargs


if TYPE_CHECKING:
    HARBOR_INSTALLED = False
    _HarborBaseAgent = _FallbackHarborBaseAgent
else:
    try:
        _HarborBaseAgent = importlib.import_module("harbor.agents.base").BaseAgent
        HARBOR_INSTALLED = True
    except ImportError:
        _HarborBaseAgent = _FallbackHarborBaseAgent
        HARBOR_INSTALLED = False


TERMINAL_MINIMAL_SYSTEM_PROMPT = """\
You are StellarWeaver running inside an isolated persistent Linux task container.
Use the `bash` tool to inspect the environment, edit files, install packages, start required
background services, and verify your work thoroughly before finishing.
For long compilations, builds, or test suites, pass an appropriate `timeout_seconds` (up to 1800).
Verify every requirement with real container commands and report concise, factual results."""


@dataclass
class HarborAgentContext:
    """Offline-compatible mirror of Harbor's AgentContext telemetry fields."""

    n_input_tokens: int | None = None
    n_cache_tokens: int | None = None
    n_output_tokens: int | None = None
    cost_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _package_version() -> str:
    try:
        return importlib.metadata.version("stellarweaver")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


def _strip_provider_prefix(model_name: str) -> str:
    return model_name.split("/", 1)[1] if "/" in model_name else model_name


def _estimate_deepseek_cost_usd(
    *,
    input_tokens: int,
    cached_input_tokens: int | None,
    output_tokens: int,
    off_peak: bool = False,
) -> float:
    """Estimate USD cost using published DeepSeek API rates (standard or 50% off-peak)."""
    hit = cached_input_tokens if cached_input_tokens is not None else 0
    miss = max(0, input_tokens - hit)
    # Standard rates: $0.006/1M cache hit, $0.30/1M cache miss, $1.20/1M output (off-peak: 50%).
    factor = 0.5 if off_peak else 1.0
    return round(
        factor * (hit * 0.006 + miss * 0.30 + output_tokens * 1.20) / 1_000_000,
        6,
    )


def build_terminal_agent(
    config: AppConfig,
    model: ModelProvider,
    store: RunJournal,
    executor: TerminalExecutor,
    *,
    on_event: EventSink | None = None,
    on_delta: DeltaSink | None = None,
    stream: bool = True,
    command_boundary: str = "harbor-container-exec",
    system_prompt: str | None = None,
    enforce_run_budget: bool = True,
) -> Agent:
    """Compose a `terminal-minimal` Agent with ONLY the container `bash` tool."""
    harness = (
        config.harness
        if config.harness.profile == "terminal-minimal"
        else terminal_minimal_harness()
    )
    engine = PolicyEngine(
        "benchmark",
        workspace=store.workspace,
        allow_terminal_container=True,
    )
    registry = ToolRegistry(
        store.workspace,
        engine,
        execution=config.execution,
        artifacts_dir=store.artifacts_dir,
        builtins=False,
        command_boundary=command_boundary,
        harness=harness,
    )
    register_terminal_tools(registry, executor)
    return Agent(
        model,
        registry,
        limits=config.limits,
        context_config=config.context,
        harness_config=harness,
        store=store,
        on_event=on_event,
        on_delta=on_delta,
        stream=stream,
        max_retries=config.retry.max_retries,
        retry_base_seconds=config.retry.base_delay_seconds,
        max_retry_delay_seconds=config.retry.max_delay_seconds,
        max_output_tokens=config.model.max_output_tokens,
        system_prompt=(
            system_prompt if system_prompt is not None else TERMINAL_MINIMAL_SYSTEM_PROMPT
        ),
        project_context=False,
        capture_host_changes=False,
        enable_web_search=False,
        enforce_run_budget=enforce_run_budget,
        adaptive_round_retention=True,
    )


class StellarWeaverHarborAgent(_HarborBaseAgent):
    """Harbor-native Agent wrapping StellarWeaver in `terminal-minimal` single-`bash` mode."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        config: AppConfig | None = None,
        provider: ModelProvider | None = None,
        env_file: Path | None = None,
        profile: Literal["terminal-minimal"] = "terminal-minimal",
        stream: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.logs_dir = Path(logs_dir)
        self.model_name = model_name
        self.profile = profile
        self._injected_provider = provider
        self._env_file = env_file
        self._stream = stream
        base_config = (
            config
            if config is not None
            else long_horizon_deepseek_flash(harness_profile="terminal-minimal")
        )
        if model_name is not None and config is None:
            normalized = _strip_provider_prefix(model_name)
            base_config = base_config.model_copy(
                update={"model": base_config.model.model_copy(update={"model": normalized})}
            )
        if base_config.harness.profile != "terminal-minimal":
            base_config = base_config.model_copy(
                update={"harness": terminal_minimal_harness()}
            )
        self.config = base_config

    @staticmethod
    def name() -> str:
        return "stellarweaver"

    def version(self) -> str | None:
        return _package_version()

    async def setup(self, environment: TerminalExecutor) -> None:
        """No host or container pre-installation required for the single-bash MVP."""
        return None

    async def run(
        self,
        instruction: str,
        environment: TerminalExecutor,
        context: Any = None,
    ) -> JSONDict:
        """Execute one Harbor trial inside `environment` and record structured telemetry."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        session_dir = self.logs_dir / "stellarweaver-session"
        session_dir.mkdir(parents=True, exist_ok=True)

        owns_provider = self._injected_provider is None
        model: ModelProvider = (
            self._injected_provider
            if self._injected_provider is not None
            else build_provider(
                self.config,
                session_dir,
                self.config.model.provider,
                None,
                env_file=self._env_file,
                stream=self._stream,
            )
        )
        started = time.monotonic()
        try:
            with SessionStore(session_dir) as store:
                agent = build_terminal_agent(
                    self.config,
                    model,
                    store,
                    environment,
                    stream=self._stream and self.config.model.supports_streaming,
                    command_boundary=f"harbor:{type(environment).__name__}",
                )
                schemas = agent.registry.schemas()
                tool_names = [schema["function"]["name"] for schema in schemas]
                tools_sha256 = digest(canonical_json(schemas))
                preamble_tokens = estimate_tokens(
                    {"system": agent.system_prompt, "tools": schemas}
                )
                manifest: JSONDict = {
                    "agent": self.name(),
                    "version": self.version(),
                    "profile": agent.harness_config.profile,
                    "tools": tool_names,
                    "tools_sha256": tools_sha256,
                    "preamble_tokens_estimated": preamble_tokens,
                    "provider": self.config.model.provider,
                    "model": self.config.model.model,
                    "thinking": self.config.model.thinking,
                    "reasoning_effort": self.config.model.reasoning_effort,
                    "context_window_tokens": self.config.model.context_window_tokens,
                    "max_output_tokens": self.config.model.max_output_tokens,
                    "limits": self.config.limits.model_dump(),
                    "execution": self.config.execution.model_dump(),
                    "harness": agent.harness_config.model_dump(),
                }
                save(self.logs_dir / "manifest.json", manifest)

                outcome = await agent.run(instruction)
                events = store.load_events()
                completed_requests = [e for e in events if e.type == "model.completed"]
                failed_requests = [e for e in events if e.type == "model.failed"]
                cached = [
                    count
                    for e in completed_requests
                    if type(count := e.payload.get("prompt_cache_hit_tokens")) is int
                ]
                cached_input_tokens: int | None = (
                    sum(cached)
                    if completed_requests and len(cached) == len(completed_requests)
                    else None
                )
                compactions = sum(1 for e in events if e.type == "context.compacted")
                length_continuations = sum(1 for e in events if e.type == "model.length_continuation")
                truncated_thinking = sum(1 for e in events if e.type == "model.truncated_thinking")
                soft_guidance_events = sum(1 for e in events if e.type == "repair.soft_guidance")
                repetition_retries = sum(
                    1
                    for e in failed_requests
                    if "repetitive generation loop" in str(e.payload.get("message", "")).lower()
                )
                cost_usd = _estimate_deepseek_cost_usd(
                    input_tokens=outcome.input_tokens,
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=outcome.output_tokens,
                )
                report: JSONDict = {
                    "agent": self.name(),
                    "version": self.version(),
                    "profile": agent.harness_config.profile,
                    "tools": tool_names,
                    "tools_sha256": tools_sha256,
                    "preamble_tokens_estimated": preamble_tokens,
                    "session_id": store.session_id,
                    "outcome": asdict(outcome),
                    "tools_invoked": [asdict(tool) for tool in agent.results],
                    "completed_model_calls": len(completed_requests),
                    "failed_model_calls": len(failed_requests),
                    "cached_input_tokens": cached_input_tokens,
                    "cache_hit_ratio": (
                        round(cached_input_tokens / outcome.input_tokens, 4)
                        if cached_input_tokens is not None and outcome.input_tokens > 0
                        else None
                    ),
                    "compactions": compactions,
                    "length_continuations": length_continuations,
                    "truncated_thinking": truncated_thinking,
                    "soft_guidance_events": soft_guidance_events,
                    "repetition_retries": repetition_retries,
                    "estimated_cost_usd": cost_usd,
                    "wall_seconds": round(time.monotonic() - started, 3),
                }
                save(self.logs_dir / "trial-report.json", report)
                self._populate_context(context, outcome, cached_input_tokens, cost_usd, report)
                return report
        finally:
            if owns_provider and isinstance(model, OpenAICompatibleChatProvider):
                await model.aclose()

    @staticmethod
    def _populate_context(
        context: Any,
        outcome: Any,
        cached_input_tokens: int | None,
        cost_usd: float,
        report: JSONDict,
    ) -> None:
        if context is None:
            return
        for attr, value in (
            ("n_input_tokens", outcome.input_tokens),
            ("n_cache_tokens", cached_input_tokens),
            ("n_output_tokens", outcome.output_tokens),
            ("cost_usd", cost_usd),
        ):
            if hasattr(context, attr):
                setattr(context, attr, value)
        if hasattr(context, "metadata"):
            existing = context.metadata
            meta = dict(existing) if isinstance(existing, dict) else {}
            meta.update(
                {
                    "profile": report["profile"],
                    "run_status": outcome.run_status,
                    "model_calls": outcome.model_calls,
                    "tool_calls": outcome.tool_calls,
                    "failed_model_calls": report["failed_model_calls"],
                    "compactions": report["compactions"],
                    "length_continuations": report["length_continuations"],
                    "truncated_thinking": report["truncated_thinking"],
                    "soft_guidance_events": report["soft_guidance_events"],
                    "repetition_retries": report["repetition_retries"],
                    "cached_input_tokens": cached_input_tokens,
                    "cache_hit_ratio": report["cache_hit_ratio"],
                }
            )
            context.metadata = meta


def aggregate_trial_reports(reports: Sequence[JSONDict]) -> JSONDict:
    """Aggregate per-trial Terminal-Bench reports into a summary dictionary."""
    total = len(reports)
    resolved = sum(1 for item in reports if item.get("resolved") is True)
    input_tokens = sum(int(item.get("outcome", {}).get("input_tokens", 0)) for item in reports)
    output_tokens = sum(int(item.get("outcome", {}).get("output_tokens", 0)) for item in reports)
    cached_values = [
        item["cached_input_tokens"]
        for item in reports
        if isinstance(item.get("cached_input_tokens"), int)
    ]
    cached_total: int | None = sum(cached_values) if total and len(cached_values) == total else None
    return {
        "tasks": total,
        "resolved": resolved,
        "resolved_rate": round(resolved / total, 4) if total else 0.0,
        "total_input_tokens": input_tokens,
        "total_cached_input_tokens": cached_total,
        "cache_hit_ratio": (
            round(cached_total / input_tokens, 4)
            if cached_total is not None and input_tokens > 0
            else None
        ),
        "total_output_tokens": output_tokens,
        "total_estimated_cost_usd": round(
            sum(float(item.get("estimated_cost_usd", 0.0)) for item in reports), 4
        ),
    }


def render_markdown_summary(reports: Sequence[JSONDict]) -> str:
    """Render a compact Markdown summary table compatible with docs/TerminalBench4Eval.md."""
    agg = aggregate_trial_reports(reports)
    cached_text = (
        str(agg["total_cached_input_tokens"])
        if agg["total_cached_input_tokens"] is not None
        else "incomplete"
    )
    ratio_text = (
        f"{agg['cache_hit_ratio'] * 100:.2f}%"
        if agg["cache_hit_ratio"] is not None
        else "incomplete"
    )
    lines = [
        "| Metric | Value |",
        "| --- | --- |",
        f"| Tasks | {agg['tasks']} |",
        f"| Resolved | {agg['resolved']} ({agg['resolved_rate'] * 100:.1f}%) |",
        f"| Total Input Tokens | {agg['total_input_tokens']:,} |",
        f"| Cached Input Tokens | {cached_text} |",
        f"| Cache Hit Ratio | {ratio_text} |",
        f"| Total Output Tokens | {agg['total_output_tokens']:,} |",
        f"| Estimated Cost (USD) | ${agg['total_estimated_cost_usd']:.4f} |",
    ]
    return "\n".join(lines) + "\n"
