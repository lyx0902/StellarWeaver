"""Harbor native Agent adapter for Terminal-Bench 4.1 (Phase 3a single-bash MVP).

Delegates container execution and lifecycle strictly to Harbor's BaseEnvironment.exec().
Does NOT orchestrate `docker run` containers or implement task verifiers.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import os
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from stellarweaver.cli import build_provider
from stellarweaver.config import (
    AppConfig,
    get_api_key,
    long_horizon_deepseek_flash,
    terminal_minimal_harness,
)
from stellarweaver.context import estimate_tokens
from stellarweaver.domain import JSONDict
from stellarweaver.evals.provenance import canonical_json
from stellarweaver.evals.swebench import execution_fingerprint, save
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
        *,
        extra_env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.logs_dir = Path(logs_dir)
        self.model_name = model_name
        self._extra_env: dict[str, str] = dict(extra_env) if extra_env else {}
        self.extra_kwargs = kwargs


if TYPE_CHECKING:
    HARBOR_INSTALLED = False
    _HarborBaseAgent = _FallbackHarborBaseAgent
    _HARBOR_CAPABILITIES: Any = None
    _HarborModelUsage: Any = None
    _harbor_policy_for: Any = None
else:
    try:
        _HarborBaseAgent = importlib.import_module("harbor.agents.base").BaseAgent
        _agent_caps = importlib.import_module("harbor.agents.capabilities").AgentCapabilities
        _HARBOR_CAPABILITIES = _agent_caps(reasoning_effort=True)
        _HarborModelUsage = importlib.import_module("harbor.models.agent.context").ModelUsage
        _harbor_policy_for = importlib.import_module("harbor.models.trial.error_policy").policy_for
        HARBOR_INSTALLED = True
    except ImportError:
        _HarborBaseAgent = _FallbackHarborBaseAgent
        _HARBOR_CAPABILITIES = None
        _HarborModelUsage = None
        _harbor_policy_for = None
        HARBOR_INSTALLED = False


_REWARD_VALID_EXCEPTIONS = frozenset(
    {
        "AgentTimeoutError",
        "ContextWindowExceededError",
        "OutputTokenExceededError",
        "ContextLengthExceededError",
        "OutputLengthExceededError",
        "AgentSafetyRefusalError",
    }
)
_TIMEOUT_EXCEPTIONS = frozenset({"AgentTimeoutError", "VerifierTimeoutError"})


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


def _package_version(name: str = "stellarweaver") -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0" if name == "stellarweaver" else None


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


def _parse_bool_flag(value: bool | str) -> bool:
    if isinstance(value, bool):
        return value
    lowered = value.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean flag value: {value!r}")


def _relative_posix(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_execution_fingerprint() -> JSONDict:
    """Capture code_sha and source_sha256 without failing if `git` is absent from PATH."""
    try:
        return execution_fingerprint()
    except (OSError, Exception):
        package = Path(__file__).resolve().parents[1]
        root = package.parents[1]
        revision: str | None = None
        head_file = root / ".git" / "HEAD"
        if head_file.is_file():
            head_text = head_file.read_text(encoding="utf-8", errors="ignore").strip()
            if head_text.startswith("ref: "):
                ref_path = root / ".git" / head_text[5:].strip()
                if ref_path.is_file():
                    revision = ref_path.read_text(encoding="utf-8", errors="ignore").strip()
            elif len(head_text) == 40:
                revision = head_text
        return {
            "code_sha": revision,
            "source_sha256": {
                path.relative_to(package).as_posix(): digest(path.read_bytes())
                for path in sorted(package.rglob("*.py"))
            },
        }


class StellarWeaverHarborAgent(_HarborBaseAgent):
    """Harbor-native Agent wrapping StellarWeaver in `terminal-minimal` single-`bash` mode."""

    if _HARBOR_CAPABILITIES is not None:
        capabilities = _HARBOR_CAPABILITIES

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        config: AppConfig | None = None,
        provider: ModelProvider | None = None,
        env_file: Path | str | None = None,
        profile: Literal["terminal-minimal"] = "terminal-minimal",
        reasoning_effort: Literal["low", "high", "max"] | None = None,
        stream: bool | str = True,
        **kwargs: Any,
    ) -> None:
        init_kwargs = dict(kwargs)
        if reasoning_effort is not None:
            init_kwargs["reasoning_effort"] = reasoning_effort
        super().__init__(logs_dir=logs_dir, model_name=model_name, **init_kwargs)
        self.logs_dir = Path(logs_dir)
        self.model_name = model_name
        self.profile = profile
        self._injected_provider = provider
        self._env_file = Path(env_file) if env_file is not None else None
        self._stream = _parse_bool_flag(stream)
        base_config = (
            config
            if config is not None
            else long_horizon_deepseek_flash(harness_profile="terminal-minimal")
        )
        if config is None:
            model_updates: dict[str, Any] = {}
            if model_name is not None:
                model_updates["model"] = _strip_provider_prefix(model_name)
            if reasoning_effort is not None:
                model_updates["reasoning_effort"] = reasoning_effort
            if model_updates:
                base_config = base_config.model_copy(
                    update={"model": base_config.model.model_copy(update=model_updates)}
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
        return _package_version("stellarweaver")

    async def setup(self, environment: TerminalExecutor) -> None:
        """No host or container pre-installation required for the single-bash MVP."""
        return None

    def _resolve_provider(self, session_dir: Path) -> ModelProvider:
        if self._injected_provider is not None:
            return self._injected_provider
        model_cfg = self.config.model.model_copy(
            update={"supports_streaming": self._stream and self.config.model.supports_streaming}
        )
        if model_cfg.provider == "fake":
            return build_provider(
                self.config,
                session_dir,
                "fake",
                None,
                env_file=self._env_file,
                stream=self._stream,
            )
        extra_env: dict[str, str] = getattr(self, "_extra_env", {}) or {}
        api_key = (
            extra_env.get(model_cfg.api_key_env)
            or get_api_key(model_cfg, session_dir, self._env_file)
            or (
                extra_env.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
                if model_cfg.provider == "deepseek"
                else None
            )
        )
        if api_key:
            return OpenAICompatibleChatProvider(model_cfg, api_key)
        return build_provider(
            self.config,
            session_dir,
            model_cfg.provider,
            None,
            env_file=self._env_file,
            stream=self._stream,
        )

    def _persist_report_and_context(
        self,
        *,
        store: SessionStore,
        agent: Agent,
        context: Any,
        tool_names: list[str],
        tools_sha256: str,
        preamble_tokens: int,
        config_sha256: str,
        harbor_version: str | None,
        started: float,
        outcome_dict: JSONDict | None,
    ) -> JSONDict:
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
        response_models = sorted(
            {
                str(model_id)
                for e in completed_requests
                if (model_id := e.payload.get("response_model")) is not None
            }
        )
        system_fingerprints = sorted(
            {
                str(fp)
                for e in completed_requests
                if (fp := e.payload.get("system_fingerprint")) is not None
            }
        )
        if outcome_dict is None:
            input_tokens = sum(
                int(e.payload.get("input_tokens", 0))
                for e in (*completed_requests, *failed_requests)
                if type(e.payload.get("input_tokens")) is int
            )
            output_tokens = sum(
                int(e.payload.get("output_tokens", 0))
                for e in (*completed_requests, *failed_requests)
                if type(e.payload.get("output_tokens")) is int
            )
            outcome_dict = {
                "run_status": "cancelled",
                "task_assessment": "unknown",
                "verification_status": "not_run",
                "evidence": (),
                "final_text": "",
                "reason": "Harbor trial cancelled or timed out",
                "model_calls": len(completed_requests) + len(failed_requests),
                "tool_calls": len(agent.results),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_estimated": False,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "session_id": store.session_id,
                "run_id": store.run_id,
                "state_path": str(store.path),
                "usage_incomplete": bool(failed_requests),
            }
        else:
            input_tokens = int(outcome_dict.get("input_tokens", 0))
            output_tokens = int(outcome_dict.get("output_tokens", 0))

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
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
        )
        report: JSONDict = {
            "agent": self.name(),
            "version": self.version(),
            "harbor_version": harbor_version,
            "profile": agent.harness_config.profile,
            "tools": tool_names,
            "tools_sha256": tools_sha256,
            "config_sha256": config_sha256,
            "preamble_tokens_estimated": preamble_tokens,
            "session_id": store.session_id,
            "session_db_relpath": _relative_posix(store.path, self.logs_dir),
            "artifacts_relpath": _relative_posix(store.artifacts_dir, self.logs_dir),
            "model": self.config.model.model,
            "response_models": response_models,
            "system_fingerprints": system_fingerprints,
            "outcome": outcome_dict,
            "tools_invoked": [asdict(tool) for tool in agent.results],
            "completed_model_calls": len(completed_requests),
            "failed_model_calls": len(failed_requests),
            "cached_input_tokens": cached_input_tokens,
            "cache_hit_ratio": (
                round(cached_input_tokens / input_tokens, 4)
                if cached_input_tokens is not None and input_tokens > 0
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
        self._populate_context(
            context,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model_name=self.config.model.model,
            report=report,
        )
        return report

    async def run(
        self,
        instruction: str,
        environment: TerminalExecutor,
        context: Any = None,
    ) -> JSONDict:
        """Execute one Harbor trial inside `environment` and record structured telemetry."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        # Keep archive directory name short to avoid Windows MAX_PATH nesting issues.
        session_dir = self.logs_dir / "sw"
        session_dir.mkdir(parents=True, exist_ok=True)

        owns_provider = self._injected_provider is None
        model: ModelProvider = self._resolve_provider(session_dir)
        harbor_version = _package_version("harbor")
        started = time.monotonic()
        try:
            with SessionStore(session_dir, data_dir=session_dir) as store:
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
                manifest_core: JSONDict = {
                    "agent": self.name(),
                    "version": self.version(),
                    "harbor_version": harbor_version,
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
                config_sha256 = digest(canonical_json(manifest_core))
                manifest: JSONDict = {
                    **manifest_core,
                    "config_sha256": config_sha256,
                    "session_id": store.session_id,
                    "session_db_relpath": _relative_posix(store.path, self.logs_dir),
                    "artifacts_relpath": _relative_posix(store.artifacts_dir, self.logs_dir),
                    **_safe_execution_fingerprint(),
                }
                save(self.logs_dir / "manifest.json", manifest)

                try:
                    outcome = await agent.run(instruction)
                except asyncio.CancelledError:
                    self._persist_report_and_context(
                        store=store,
                        agent=agent,
                        context=context,
                        tool_names=tool_names,
                        tools_sha256=tools_sha256,
                        preamble_tokens=preamble_tokens,
                        config_sha256=config_sha256,
                        harbor_version=harbor_version,
                        started=started,
                        outcome_dict=None,
                    )
                    raise

                report = self._persist_report_and_context(
                    store=store,
                    agent=agent,
                    context=context,
                    tool_names=tool_names,
                    tools_sha256=tools_sha256,
                    preamble_tokens=preamble_tokens,
                    config_sha256=config_sha256,
                    harbor_version=harbor_version,
                    started=started,
                    outcome_dict=asdict(outcome),
                )
                if outcome.run_status == "cancelled":
                    # Propagate cancellation so Harbor's asyncio.wait_for records AgentTimeoutError.
                    raise asyncio.CancelledError("Harbor trial cancelled or timed out")
                return report
        finally:
            if owns_provider and isinstance(model, OpenAICompatibleChatProvider):
                await model.aclose()

    def populate_context_post_run(self, context: Any) -> None:
        """Backfill Harbor AgentContext from persisted trial-report.json if empty."""
        report_path = self.logs_dir / "trial-report.json"
        if context is None or not report_path.is_file():
            return
        report = json.loads(report_path.read_text(encoding="utf-8"))
        outcome = report.get("outcome", {})
        self._populate_context(
            context,
            input_tokens=int(outcome.get("input_tokens", 0)),
            cached_input_tokens=report.get("cached_input_tokens"),
            output_tokens=int(outcome.get("output_tokens", 0)),
            cost_usd=float(report.get("estimated_cost_usd", 0.0)),
            model_name=str(report.get("model") or self.config.model.model),
            report=report,
        )

    @staticmethod
    def _populate_context(
        context: Any,
        *,
        input_tokens: int,
        cached_input_tokens: int | None,
        output_tokens: int,
        cost_usd: float,
        model_name: str,
        report: JSONDict,
    ) -> None:
        if context is None:
            return
        outcome = report.get("outcome", {})
        for attr, value in (
            ("n_input_tokens", input_tokens),
            ("n_cache_tokens", cached_input_tokens),
            ("n_output_tokens", output_tokens),
            ("cost_usd", cost_usd),
        ):
            if hasattr(context, attr):
                setattr(context, attr, value)
        if hasattr(context, "model_usage") and _HarborModelUsage is not None:
            context.model_usage = {
                model_name: _HarborModelUsage(
                    n_input_tokens=input_tokens,
                    n_cache_tokens=cached_input_tokens or 0,
                    n_output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            }
        if hasattr(context, "metadata"):
            existing = context.metadata
            meta = dict(existing) if isinstance(existing, dict) else {}
            meta.update(
                {
                    "profile": report["profile"],
                    "run_status": outcome.get("run_status"),
                    "model_calls": outcome.get("model_calls"),
                    "tool_calls": outcome.get("tool_calls"),
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


def _exception_allows_valid_reward(exception_type: str) -> bool:
    if _harbor_policy_for is not None:
        return bool(_harbor_policy_for(exception_type).reward_valid)
    return exception_type in _REWARD_VALID_EXCEPTIONS


def _extract_primary_reward(rewards: Any) -> float | None:
    if not isinstance(rewards, dict) or not rewards:
        return None
    preferred = rewards.get("reward")
    if isinstance(preferred, (int, float)) and not isinstance(preferred, bool):
        return float(preferred)
    for value in rewards.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _classify_harbor_verdict(
    result_data: JSONDict,
) -> tuple[Literal["passed", "failed", "timeout", "unscored"], bool | None, float | None, bool, str | None]:
    """Classify Harbor TrialResult per Harbor 0.25.0 reward_valid semantics.

    Priority:
      1. reward_valid and reward >= 1.0 -> ("passed", True)  (still preserves exception_type)
      2. AgentTimeoutError / VerifierTimeoutError -> ("timeout", False)
      3. reward_valid and reward < 1.0 -> ("failed", False)
      4. Otherwise -> ("unscored", None)
    """
    recorded_exceptions: list[str] = []
    top_exc = result_data.get("exception_info")
    if isinstance(top_exc, dict) and isinstance(top_exc.get("exception_type"), str):
        recorded_exceptions.append(top_exc["exception_type"])
    for step in result_data.get("step_results") or []:
        if isinstance(step, dict):
            step_exc = step.get("exception_info")
            if isinstance(step_exc, dict) and isinstance(step_exc.get("exception_type"), str):
                recorded_exceptions.append(step_exc["exception_type"])

    primary_exception = recorded_exceptions[0] if recorded_exceptions else None
    verifier = result_data.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    reward = _extract_primary_reward(rewards)
    reward_valid = bool(
        isinstance(rewards, dict)
        and bool(rewards)
        and reward is not None
        and all(_exception_allows_valid_reward(exc) for exc in recorded_exceptions)
    )

    if reward_valid and reward is not None and reward >= 1.0:
        return "passed", True, reward, True, primary_exception
    if any(exc in _TIMEOUT_EXCEPTIONS for exc in recorded_exceptions):
        return "timeout", False, reward, reward_valid, primary_exception
    if reward_valid and reward is not None and reward < 1.0:
        return "failed", False, reward, True, primary_exception
    return "unscored", None, reward, reward_valid, primary_exception


def merge_harbor_trial(trial_dir: Path) -> JSONDict:
    """Merge Harbor's `<trial_dir>/result.json` with StellarWeaver's `agent/trial-report.json`."""
    trial_path = Path(trial_dir)
    result_path = trial_path / "result.json"
    agent_report_path = trial_path / "agent" / "trial-report.json"
    agent_manifest_path = trial_path / "agent" / "manifest.json"

    result_data: JSONDict = (
        json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    )
    agent_report: JSONDict = (
        json.loads(agent_report_path.read_text(encoding="utf-8"))
        if agent_report_path.is_file()
        else {}
    )
    agent_manifest: JSONDict = (
        json.loads(agent_manifest_path.read_text(encoding="utf-8"))
        if agent_manifest_path.is_file()
        else {}
    )

    verdict, resolved, reward, reward_valid, exception_type = _classify_harbor_verdict(result_data)
    merged: JSONDict = {
        **agent_report,
        "trial_dir": trial_path.name,
        "trial_id": result_data.get("id"),
        "trial_name": result_data.get("trial_name") or trial_path.name,
        "task_name": result_data.get("task_name"),
        "task_id": result_data.get("task_id"),
        "task_checksum": result_data.get("task_checksum"),
        "verifier_result": result_data.get("verifier_result"),
        "exception_type": exception_type,
        "exception_info": result_data.get("exception_info"),
        "reward": reward,
        "reward_valid": reward_valid,
        "verdict": verdict,
        "resolved": resolved,
        "harbor_result_present": result_path.is_file(),
        "agent_report_present": agent_report_path.is_file(),
    }
    if agent_manifest and "code_sha" not in merged:
        merged["code_sha"] = agent_manifest.get("code_sha")
    if "outcome" not in merged:
        agent_res = result_data.get("agent_result") or {}
        merged["outcome"] = {
            "input_tokens": int(agent_res.get("n_input_tokens") or 0),
            "output_tokens": int(agent_res.get("n_output_tokens") or 0),
        }
        if "cached_input_tokens" not in merged and isinstance(agent_res.get("n_cache_tokens"), int):
            merged["cached_input_tokens"] = agent_res["n_cache_tokens"]
        if "estimated_cost_usd" not in merged and isinstance(agent_res.get("cost_usd"), (int, float)):
            merged["estimated_cost_usd"] = float(agent_res["cost_usd"])
    return merged


def collect_harbor_job_reports(job_dir: Path) -> list[JSONDict]:
    """Discover all Harbor trial directories under `job_dir` and merge their verdicts."""
    root = Path(job_dir)
    trial_dirs: list[Path] = []
    if (root / "result.json").is_file() and (root / "agent").is_dir():
        trial_dirs.append(root)
    else:
        for child in sorted(root.iterdir()):
            if child.is_dir() and (
                (child / "result.json").is_file()
                or (child / "agent" / "trial-report.json").is_file()
            ):
                trial_dirs.append(child)
    return [merge_harbor_trial(trial_dir) for trial_dir in trial_dirs]


def _item_verdict(item: JSONDict) -> Literal["passed", "failed", "timeout", "unscored"]:
    verdict = item.get("verdict")
    if verdict == "passed":
        return "passed"
    if verdict == "failed":
        return "failed"
    if verdict == "timeout":
        return "timeout"
    if verdict == "unscored":
        return "unscored"
    if item.get("resolved") is True:
        return "passed"
    if item.get("resolved") is False:
        return "failed"
    return "unscored"


def aggregate_trial_reports(reports: Sequence[JSONDict]) -> JSONDict:
    """Aggregate per-trial Terminal-Bench reports (including merged Harbor verdicts)."""
    total = len(reports)
    counts = {"passed": 0, "failed": 0, "timeout": 0, "unscored": 0}
    for item in reports:
        counts[_item_verdict(item)] += 1
    resolved = counts["passed"]
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
        "passed": counts["passed"],
        "failed": counts["failed"],
        "timeout": counts["timeout"],
        "unscored": counts["unscored"],
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
        f"| Resolved (Passed) | {agg['passed']} ({agg['resolved_rate'] * 100:.1f}%) |",
        f"| Failed | {agg['failed']} |",
        f"| Timeout | {agg['timeout']} |",
        f"| Unscored | {agg['unscored']} |",
        f"| Total Input Tokens | {agg['total_input_tokens']:,} |",
        f"| Cached Input Tokens | {cached_text} |",
        f"| Cache Hit Ratio | {ratio_text} |",
        f"| Total Output Tokens | {agg['total_output_tokens']:,} |",
        f"| Estimated Cost (USD) | ${agg['total_estimated_cost_usd']:.4f} |",
    ]
    return "\n".join(lines) + "\n"
