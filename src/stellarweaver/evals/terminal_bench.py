"""Harbor native Agent adapter for Terminal-Bench 4.1 (Phase 3a single-bash MVP).

Delegates container execution and lifecycle strictly to Harbor's BaseEnvironment.exec().
Does NOT orchestrate `docker run` containers or implement task verifiers.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from stellarweaver.cli import build_provider
from stellarweaver.config import (
    AppConfig,
    CompletionEvidenceReminderVersion,
    get_api_key,
    long_horizon_deepseek_flash,
    terminal_minimal_harness,
)
from stellarweaver.context import estimate_tokens
from stellarweaver.domain import AgentEvent, JSONDict
from stellarweaver.evals.provenance import canonical_json
from stellarweaver.evals.swebench import execution_fingerprint, save
from stellarweaver.evals.swebench_pilot import digest
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ModelProvider
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.loop import (
    COMPLETION_EVIDENCE_REMINDERS,
    Agent,
    DeltaSink,
    EventSink,
    resolve_completion_evidence_reminder,
)
from stellarweaver.session.journal import RunJournal
from stellarweaver.session.store import SessionStore
from stellarweaver.session.store import _readonly as _open_readonly_session_db
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
    _HarborTrialLock: Any = None
    _harbor_policy_for: Any = None
else:
    try:
        _HarborBaseAgent = importlib.import_module("harbor.agents.base").BaseAgent
        _agent_caps = importlib.import_module("harbor.agents.capabilities").AgentCapabilities
        _HARBOR_CAPABILITIES = _agent_caps(reasoning_effort=True)
        _HarborModelUsage = importlib.import_module("harbor.models.agent.context").ModelUsage
        _HarborTrialLock = importlib.import_module("harbor.models.job.lock").TrialLock
        _harbor_policy_for = importlib.import_module("harbor.models.trial.error_policy").policy_for
        HARBOR_INSTALLED = True
    except ImportError:
        _HarborBaseAgent = _FallbackHarborBaseAgent
        _HARBOR_CAPABILITIES = None
        _HarborModelUsage = None
        _HarborTrialLock = None
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
    model_usage: dict[str, Any] | None = None
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


def _parse_reminder_version(value: str) -> CompletionEvidenceReminderVersion:
    normalized = value.strip().lower()
    if normalized not in COMPLETION_EVIDENCE_REMINDERS:
        valid = ", ".join(sorted(COMPLETION_EVIDENCE_REMINDERS))
        raise ValueError(f"Invalid completion_evidence_reminder_version: {value!r} (expected one of: {valid})")
    return cast(CompletionEvidenceReminderVersion, normalized)


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


def _classify_event_input_source(payload: JSONDict) -> Literal["reported", "estimated", "unknown"]:
    input_est = payload.get("input_estimated")
    est = payload.get("estimated")
    if input_est is False or (input_est is None and est is False):
        return "reported"
    if input_est is True:
        return "estimated"
    usage = payload.get("usage")
    if isinstance(usage, dict):
        raw_prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        if type(raw_prompt) is int and raw_prompt >= 0:
            return "reported"
    return "unknown"


def _classify_event_output_source(payload: JSONDict) -> Literal["reported", "estimated", "unknown"]:
    output_est = payload.get("output_estimated")
    if output_est is False:
        return "reported"
    if output_est is True:
        return "estimated"
    est = payload.get("estimated")
    if est is False:
        return "reported"
    input_est = payload.get("input_estimated")
    if input_est is False and est is True:
        return "estimated"
    usage = payload.get("usage")
    if isinstance(usage, dict):
        raw_comp = usage.get("completion_tokens", usage.get("output_tokens"))
        if type(raw_comp) is int and raw_comp >= 0:
            return "reported"
    return "unknown"


def _summarize_usage_events(
    events: Sequence[AgentEvent], *, source: str = "events"
) -> JSONDict:
    """Summarize usage strictly by per-field provenance (reported vs estimated vs unknown)."""
    reported_input_tokens = 0
    reported_output_tokens = 0
    reported_cached_input_tokens = 0
    reported_input_requests = 0
    reported_output_requests = 0
    reported_cache_requests = 0

    estimated_input_tokens = 0
    estimated_output_tokens = 0
    estimated_input_requests = 0
    estimated_output_requests = 0

    failed_estimated_input_tokens = 0
    failed_estimated_output_tokens = 0
    completed_estimated_input_requests = 0
    completed_estimated_output_requests = 0

    unknown_input_tokens = 0
    unknown_output_tokens = 0
    unknown_input_requests = 0
    unknown_output_requests = 0
    completed_unknown_input_requests = 0
    completed_unknown_output_requests = 0

    completed_calls = 0
    failed_calls = 0
    post_compaction_uncached_inputs: list[int] = []
    awaiting_post_compaction = False
    response_models: set[str] = set()
    system_fingerprints: set[str] = set()

    for event in events:
        if event.type == "context.compacted":
            awaiting_post_compaction = True
            continue
        if event.type not in {"model.completed", "model.failed"}:
            continue
        payload = event.payload
        is_failed = event.type == "model.failed"
        if is_failed:
            failed_calls += 1
        else:
            completed_calls += 1
            if (model_id := payload.get("response_model")) is not None:
                response_models.add(str(model_id))
            if (fp := payload.get("system_fingerprint")) is not None:
                system_fingerprints.add(str(fp))

        inp = payload.get("input_tokens")
        inp_val = inp if type(inp) is int and inp >= 0 else None
        in_source = _classify_event_input_source(payload) if inp_val is not None else "unknown"
        if in_source == "reported" and inp_val is not None:
            reported_input_tokens += inp_val
            reported_input_requests += 1
            cache_hit = payload.get("prompt_cache_hit_tokens")
            if type(cache_hit) is int and cache_hit >= 0:
                reported_cached_input_tokens += cache_hit
                reported_cache_requests += 1
            if not is_failed and awaiting_post_compaction:
                if type(cache_hit) is int and cache_hit >= 0:
                    post_compaction_uncached_inputs.append(max(0, inp_val - cache_hit))
                awaiting_post_compaction = False
        elif in_source == "estimated" and inp_val is not None:
            estimated_input_tokens += inp_val
            estimated_input_requests += 1
            if is_failed:
                failed_estimated_input_tokens += inp_val
            else:
                completed_estimated_input_requests += 1
                awaiting_post_compaction = False
        else:
            if inp_val is not None:
                unknown_input_tokens += inp_val
            unknown_input_requests += 1
            if not is_failed:
                completed_unknown_input_requests += 1
                awaiting_post_compaction = False

        out = payload.get("output_tokens")
        out_val = out if type(out) is int and out >= 0 else None
        out_source = _classify_event_output_source(payload) if out_val is not None else "unknown"
        if out_source == "reported" and out_val is not None:
            reported_output_tokens += out_val
            reported_output_requests += 1
        elif out_source == "estimated" and out_val is not None:
            estimated_output_tokens += out_val
            estimated_output_requests += 1
            if is_failed:
                failed_estimated_output_tokens += out_val
            else:
                completed_estimated_output_requests += 1
        else:
            if out_val is not None:
                unknown_output_tokens += out_val
            unknown_output_requests += 1
            if not is_failed:
                completed_unknown_output_requests += 1

    total_requests = completed_calls + failed_calls
    input_usage_complete = (
        completed_estimated_input_requests == 0
        and completed_unknown_input_requests == 0
        and (reported_input_requests > 0 or total_requests == 0)
    )
    output_usage_complete = (
        completed_estimated_output_requests == 0
        and completed_unknown_output_requests == 0
        and (reported_output_requests > 0 or total_requests == 0)
    )
    cache_usage_complete = (
        input_usage_complete
        and reported_input_requests > 0
        and reported_cache_requests == reported_input_requests
    )
    cache_hit_ratio = (
        round(reported_cached_input_tokens / reported_input_tokens, 4)
        if cache_usage_complete and reported_input_tokens > 0
        else None
    )
    cache_consistent_for_cost = reported_cache_requests in {0, reported_input_requests}
    reported_cost_usd: float | None = (
        _estimate_deepseek_cost_usd(
            input_tokens=reported_input_tokens,
            cached_input_tokens=(reported_cached_input_tokens if cache_usage_complete else 0),
            output_tokens=reported_output_tokens,
        )
        if input_usage_complete and output_usage_complete and cache_consistent_for_cost
        else None
    )
    failed_estimated_cost_usd = _estimate_deepseek_cost_usd(
        input_tokens=failed_estimated_input_tokens,
        cached_input_tokens=0,
        output_tokens=failed_estimated_output_tokens,
    )
    return {
        "usage_accounting_version": 2,
        "usage_accounting_source": source,
        "response_models": sorted(response_models),
        "system_fingerprints": sorted(system_fingerprints),
        "completed_model_calls": completed_calls,
        "failed_model_calls": failed_calls,
        "reported_input_tokens": reported_input_tokens,
        "reported_output_tokens": reported_output_tokens,
        "reported_cached_input_tokens": (
            reported_cached_input_tokens if reported_cache_requests > 0 else None
        ),
        "reported_estimated_cost_usd": reported_cost_usd,
        "input_usage_complete": input_usage_complete,
        "output_usage_complete": output_usage_complete,
        "cache_usage_complete": cache_usage_complete,
        "usage_coverage": {
            "total_requests": total_requests,
            "reported_input_requests": reported_input_requests,
            "estimated_input_requests": estimated_input_requests,
            "unknown_input_requests": unknown_input_requests,
            "reported_cache_requests": reported_cache_requests,
            "reported_output_requests": reported_output_requests,
            "estimated_output_requests": estimated_output_requests,
            "unknown_output_requests": unknown_output_requests,
        },
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "failed_estimated_input_tokens": failed_estimated_input_tokens,
        "failed_estimated_output_tokens": failed_estimated_output_tokens,
        "failed_estimated_cost_usd": failed_estimated_cost_usd,
        "unknown_input_tokens": unknown_input_tokens,
        "unknown_output_tokens": unknown_output_tokens,
        "post_compaction_uncached_input_tokens": sum(post_compaction_uncached_inputs),
        "post_compaction_first_request_count": len(post_compaction_uncached_inputs),
        "cached_input_tokens": (reported_cached_input_tokens if cache_usage_complete else None),
        "cache_hit_ratio": cache_hit_ratio,
        "estimated_cost_usd": reported_cost_usd,
    }


def _extract_outcome_run_id(outcome: JSONDict | None) -> str | None:
    if not isinstance(outcome, dict):
        return None
    val = outcome.get("run_id")
    return val if isinstance(val, str) and val else None


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
        completion_evidence_reminder: bool | str | None = None,
        completion_evidence_reminder_version: str | None = None,
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
        harness_updates: dict[str, Any] = {}
        if completion_evidence_reminder is not None:
            harness_updates["completion_evidence_reminder"] = _parse_bool_flag(
                completion_evidence_reminder
            )
        if completion_evidence_reminder_version is not None:
            harness_updates["completion_evidence_reminder_version"] = _parse_reminder_version(
                completion_evidence_reminder_version
            )
        if harness_updates:
            base_config = base_config.model_copy(
                update={"harness": base_config.harness.model_copy(update=harness_updates)}
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
        ended_ns = time.monotonic_ns()
        events = store.load_events()
        run_id = _extract_outcome_run_id(outcome_dict) or store.run_id
        run_events = [e for e in events if not e.run_id or e.run_id == run_id]
        usage_summary = _summarize_usage_events(run_events, source="events")
        failed_requests = [e for e in run_events if e.type == "model.failed"]

        if outcome_dict is None:
            input_tokens = (
                int(usage_summary["reported_input_tokens"])
                + int(usage_summary["estimated_input_tokens"])
                + int(usage_summary["unknown_input_tokens"])
            )
            output_tokens = (
                int(usage_summary["reported_output_tokens"])
                + int(usage_summary["estimated_output_tokens"])
                + int(usage_summary["unknown_output_tokens"])
            )
            outcome_dict = {
                "run_status": "cancelled",
                "task_assessment": "unknown",
                "verification_status": "not_run",
                "evidence": (),
                "final_text": "",
                "reason": "Harbor trial cancelled or timed out",
                "model_calls": (
                    int(usage_summary["completed_model_calls"])
                    + int(usage_summary["failed_model_calls"])
                ),
                "tool_calls": len(agent.results),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usage_estimated": not (
                    usage_summary["input_usage_complete"]
                    and usage_summary["output_usage_complete"]
                ),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "session_id": store.session_id,
                "run_id": store.run_id,
                "state_path": str(store.path),
                "usage_incomplete": bool(failed_requests),
            }

        compactions = sum(1 for e in run_events if e.type == "context.compacted")
        length_continuations = sum(
            1 for e in run_events if e.type == "model.length_continuation"
        )
        truncated_thinking = sum(1 for e in run_events if e.type == "model.truncated_thinking")
        soft_guidance_events = sum(1 for e in run_events if e.type == "repair.soft_guidance")
        repetition_retries = sum(
            1
            for e in failed_requests
            if "repetitive generation loop" in str(e.payload.get("message", "")).lower()
        )
        unexpected_prefix_divergences = sum(
            1 for e in run_events if e.type == "cache.prefix_diverged"
        )
        reminder = next(
            (event for event in run_events if event.type == "completion_evidence.reminded"), None
        )
        after_reminder = (
            [event for event in run_events if event.seq > reminder.seq] if reminder else []
        )
        reminder_usage = {
            "completion_evidence_reminders": int(reminder is not None),
            "post_reminder_model_calls": sum(
                event.type in {"model.completed", "model.failed"} for event in after_reminder
            ),
            "post_reminder_tool_calls": sum(event.type == "tool.completed" for event in after_reminder),
            "post_reminder_wall_seconds": (
                round((ended_ns - int(reminder.payload["monotonic_ns"])) / 1_000_000_000, 3)
                if reminder
                else 0.0
            ),
        }
        if reminder is not None:
            store.append_event(
                "completion_evidence.finished", {**reminder_usage, "reminder_seq": reminder.seq}
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
            "run_id": run_id,
            "session_db_relpath": _relative_posix(store.path, self.logs_dir),
            "artifacts_relpath": _relative_posix(store.artifacts_dir, self.logs_dir),
            "model": self.config.model.model,
            "outcome": outcome_dict,
            "tools_invoked": [asdict(tool) for tool in agent.results],
            **usage_summary,
            **reminder_usage,
            "compactions": compactions,
            "length_continuations": length_continuations,
            "truncated_thinking": truncated_thinking,
            "soft_guidance_events": soft_guidance_events,
            "repetition_retries": repetition_retries,
            "unexpected_prefix_divergences": unexpected_prefix_divergences,
            "wall_seconds": round(time.monotonic() - started, 3),
        }
        save(self.logs_dir / "trial-report.json", report)
        self._populate_context_from_report(
            context, report, model_name=self.config.model.model
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
                reminder_version, reminder_text = resolve_completion_evidence_reminder(
                    agent.harness_config
                )
                manifest_core: JSONDict = {
                    "agent": self.name(),
                    "version": self.version(),
                    "harbor_version": harbor_version,
                    "profile": agent.harness_config.profile,
                    "tools": tool_names,
                    "tools_sha256": tools_sha256,
                    "system_prompt_sha256": digest(agent.system_prompt.encode("utf-8")),
                    "preamble_tokens_estimated": preamble_tokens,
                    "provider": self.config.model.provider,
                    "model": self.config.model.model,
                    "thinking": self.config.model.thinking,
                    "reasoning_effort": self.config.model.reasoning_effort,
                    "context_window_tokens": self.config.model.context_window_tokens,
                    "max_output_tokens": self.config.model.max_output_tokens,
                    "context": self.config.context.model_dump(),
                    "retry": self.config.retry.model_dump(),
                    "limits": self.config.limits.model_dump(),
                    "execution": self.config.execution.model_dump(),
                    "harness": agent.harness_config.model_dump(),
                    "completion_evidence_reminder_version": reminder_version,
                    "completion_evidence_reminder_sha256": (
                        digest(reminder_text.encode("utf-8"))
                        if reminder_text is not None
                        else None
                    ),
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
        report = _read_json_dict(report_path)
        if not report:
            return
        if report.get("usage_accounting_version") != 2:
            report = _enrich_legacy_agent_report(self.logs_dir, report)
        self._populate_context_from_report(
            context,
            report,
            model_name=str(report.get("model") or self.config.model.model),
        )

    @classmethod
    def _populate_context_from_report(
        cls,
        context: Any,
        report: JSONDict,
        *,
        model_name: str,
    ) -> None:
        if report.get("usage_accounting_source") == "legacy_mixed_unverified":
            input_tokens: int | None = None
            cached_input_tokens: int | None = None
            output_tokens: int | None = None
            cost_usd: float | None = None
        else:
            raw_in = report.get("reported_input_tokens")
            raw_out = report.get("reported_output_tokens")
            raw_cached = report.get("reported_cached_input_tokens")
            raw_cost = report.get("reported_estimated_cost_usd")
            input_tokens = raw_in if report.get("input_usage_complete") and type(raw_in) is int else None
            output_tokens = (
                raw_out if report.get("output_usage_complete") and type(raw_out) is int else None
            )
            cached_input_tokens = (
                raw_cached if report.get("cache_usage_complete") and type(raw_cached) is int else None
            )
            cost_usd = (
                float(raw_cost)
                if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                else None
            )
        cls._populate_context(
            context,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model_name=model_name,
            report=report,
        )

    @staticmethod
    def _populate_context(
        context: Any,
        *,
        input_tokens: int | None,
        cached_input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
        model_name: str,
        report: JSONDict,
    ) -> None:
        if context is None:
            return
        outcome = _as_dict(report.get("outcome"))
        for attr, value in (
            ("n_input_tokens", input_tokens),
            ("n_cache_tokens", cached_input_tokens),
            ("n_output_tokens", output_tokens),
            ("cost_usd", cost_usd),
        ):
            if hasattr(context, attr):
                setattr(context, attr, value)
        if hasattr(context, "model_usage") and _HarborModelUsage is not None:
            if input_tokens is not None and output_tokens is not None:
                context.model_usage = {
                    model_name: _HarborModelUsage(
                        n_input_tokens=input_tokens,
                        n_cache_tokens=cached_input_tokens or 0,
                        n_output_tokens=output_tokens,
                        cost_usd=cost_usd,
                    )
                }
            else:
                context.model_usage = None
        if hasattr(context, "metadata"):
            existing = context.metadata
            meta = dict(existing) if isinstance(existing, dict) else {}
            meta.update(
                {
                    "profile": report.get("profile"),
                    "run_status": outcome.get("run_status"),
                    "model_calls": outcome.get("model_calls"),
                    "tool_calls": outcome.get("tool_calls"),
                    "failed_model_calls": report.get("failed_model_calls"),
                    "compactions": report.get("compactions"),
                    "length_continuations": report.get("length_continuations"),
                    "truncated_thinking": report.get("truncated_thinking"),
                    "soft_guidance_events": report.get("soft_guidance_events"),
                    "repetition_retries": report.get("repetition_retries"),
                    "unexpected_prefix_divergences": report.get("unexpected_prefix_divergences"),
                    "cached_input_tokens": cached_input_tokens,
                    "cache_hit_ratio": report.get("cache_hit_ratio"),
                    "usage_accounting_source": report.get("usage_accounting_source"),
                    "input_usage_complete": report.get("input_usage_complete"),
                    "output_usage_complete": report.get("output_usage_complete"),
                    "cache_usage_complete": report.get("cache_usage_complete"),
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


def _read_json_dict(path: Path) -> JSONDict:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _as_dict(value: Any) -> JSONDict:
    return value if isinstance(value, dict) else {}


def _harbor_trial_lock(entry: JSONDict) -> Any | None:
    if _HarborTrialLock is None or not entry:
        return None
    try:
        return _HarborTrialLock.model_validate(entry)
    except Exception:
        return None


def _parse_ctrf_summary(trial_path: Path) -> JSONDict | None:
    """Optionally parse `verifier/ctrf.json` test counts as diagnostic metadata."""
    payload = _read_json_dict(trial_path / "verifier" / "ctrf.json")
    results = payload.get("results")
    if not isinstance(results, dict):
        return None
    summary = results.get("summary")
    if not isinstance(summary, dict):
        return None
    tests = summary.get("tests")
    passed = summary.get("passed")
    if type(tests) is not int or type(passed) is not int:
        return None
    failed = summary.get("failed")
    skipped = summary.get("skipped")
    return {
        "total": tests,
        "passed": passed,
        "failed": failed if type(failed) is int else max(0, tests - passed),
        "skipped": skipped if type(skipped) is int else 0,
    }


def _mark_legacy_mixed_unverified(agent_report: JSONDict) -> JSONDict:
    outcome = _as_dict(agent_report.get("outcome"))
    return {
        **agent_report,
        "usage_accounting_source": "legacy_mixed_unverified",
        "reported_input_tokens": None,
        "reported_output_tokens": None,
        "reported_cached_input_tokens": None,
        "reported_estimated_cost_usd": None,
        "input_usage_complete": False,
        "output_usage_complete": False,
        "cache_usage_complete": False,
        "cached_input_tokens": None,
        "cache_hit_ratio": None,
        "estimated_cost_usd": None,
        "legacy_mixed_input_tokens": int(outcome.get("input_tokens") or 0),
        "legacy_mixed_output_tokens": int(outcome.get("output_tokens") or 0),
        "legacy_mixed_cached_input_tokens": (
            agent_report.get("cached_input_tokens")
            if type(agent_report.get("cached_input_tokens")) is int
            else None
        ),
        "legacy_mixed_cost_usd": (
            float(cost)
            if isinstance(cost := agent_report.get("estimated_cost_usd"), (int, float))
            and not isinstance(cost, bool)
            else None
        ),
    }


def _enrich_legacy_agent_report(
    logs_dir: Path, agent_report: JSONDict, *, confined_root: Path | None = None
) -> JSONDict:
    """Safely rebuild per-field usage from confined WAL-safe SQLite events when verifiable."""
    relpath = agent_report.get("session_db_relpath")
    if not isinstance(relpath, str) or not relpath or Path(relpath).is_absolute():
        return _mark_legacy_mixed_unverified(agent_report)
    root_resolved = (confined_root if confined_root is not None else logs_dir).resolve()
    try:
        candidate = (logs_dir / relpath).resolve()
    except OSError:
        return _mark_legacy_mixed_unverified(agent_report)
    if not candidate.is_relative_to(root_resolved) or not candidate.is_file():
        return _mark_legacy_mixed_unverified(agent_report)

    expected_sid = agent_report.get("session_id")
    expected_rid = agent_report.get("run_id") or _extract_outcome_run_id(
        _as_dict(agent_report.get("outcome"))
    )
    try:
        with contextlib.closing(_open_readonly_session_db(candidate)) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version == 1:
                row = conn.execute("SELECT DISTINCT session_id FROM runs").fetchone()
                db_sid = row[0] if row is not None else None
            else:
                row = conn.execute("SELECT session_id FROM sessions").fetchone()
                db_sid = row[0] if row is not None else None
            if isinstance(expected_sid, str) and expected_sid and db_sid != expected_sid:
                return _mark_legacy_mixed_unverified(agent_report)
            if isinstance(expected_rid, str) and expected_rid:
                run_row = conn.execute(
                    "SELECT 1 FROM runs WHERE run_id=? LIMIT 1", (expected_rid,)
                ).fetchone()
                if run_row is None:
                    return _mark_legacy_mixed_unverified(agent_report)
            events = [
                AgentEvent(**json.loads(ev_row[0]))
                for ev_row in conn.execute("SELECT event_json FROM events ORDER BY seq")
            ]
    except Exception:
        return _mark_legacy_mixed_unverified(agent_report)

    if isinstance(expected_rid, str) and expected_rid:
        matching_events = [e for e in events if not e.run_id or e.run_id == expected_rid]
    else:
        matching_events = events
    if not any(e.type in {"model.completed", "model.failed"} for e in matching_events):
        return _mark_legacy_mixed_unverified(agent_report)
    rebuilt = _summarize_usage_events(matching_events, source="legacy_sqlite_rebuilt")
    return {**agent_report, **rebuilt}


@dataclass(frozen=True)
class _TrialMatchFields:
    task_name: str | None
    task_digest: str | None
    agent_name: str | None
    model_name: str | None
    kwargs_json: str | None
    env_type: str | None
    env_force_build: bool | None
    env_delete: bool | None
    verifier_disable: bool | None
    verifier_env_mode: str | None


def _extract_fields_from_lock_entry(entry: JSONDict) -> _TrialMatchFields:
    task = _as_dict(entry.get("task"))
    agent = _as_dict(entry.get("agent"))
    env = _as_dict(entry.get("environment"))
    verifier = _as_dict(entry.get("verifier"))
    raw_task_name = task.get("name")
    raw_digest = task.get("digest") or task.get("ref")
    raw_agent_name = agent.get("name")
    raw_model_name = agent.get("model_name")
    raw_kwargs = agent.get("kwargs") if "kwargs" in agent else None
    raw_env_type = env.get("type")
    raw_force_build = env.get("force_build")
    raw_delete = env.get("delete")
    raw_v_disable = verifier.get("disable")
    raw_v_mode = verifier.get("environment_mode")
    return _TrialMatchFields(
        task_name=str(raw_task_name) if isinstance(raw_task_name, str) and raw_task_name else None,
        task_digest=str(raw_digest) if isinstance(raw_digest, str) and raw_digest else None,
        agent_name=str(raw_agent_name) if isinstance(raw_agent_name, str) and raw_agent_name else None,
        model_name=str(raw_model_name) if isinstance(raw_model_name, str) and raw_model_name else None,
        kwargs_json=(
            canonical_json(raw_kwargs).decode("utf-8") if isinstance(raw_kwargs, dict) else None
        ),
        env_type=str(raw_env_type) if isinstance(raw_env_type, str) and raw_env_type else None,
        env_force_build=raw_force_build if type(raw_force_build) is bool else None,
        env_delete=raw_delete if type(raw_delete) is bool else None,
        verifier_disable=raw_v_disable if type(raw_v_disable) is bool else None,
        verifier_env_mode=str(raw_v_mode) if isinstance(raw_v_mode, str) and raw_v_mode else None,
    )


def _extract_fields_from_disk_trial(
    result_data: JSONDict, agent_manifest: JSONDict
) -> _TrialMatchFields:
    config = _as_dict(result_data.get("config"))
    cfg_task = _as_dict(config.get("task"))
    cfg_agent = _as_dict(config.get("agent"))
    cfg_env = _as_dict(config.get("environment"))
    cfg_verifier = _as_dict(config.get("verifier"))
    task_id = _as_dict(result_data.get("task_id"))
    agent_info = _as_dict(result_data.get("agent_info"))
    model_info = _as_dict(agent_info.get("model_info"))

    raw_task_name = result_data.get("task_name") or cfg_task.get("name")
    raw_digest = cfg_task.get("ref") or cfg_task.get("digest") or task_id.get("ref")
    raw_agent_name = cfg_agent.get("name") or agent_info.get("name") or agent_manifest.get("agent")
    raw_model_name = cfg_agent.get("model_name") or model_info.get("name") or agent_manifest.get("model")
    raw_kwargs = cfg_agent.get("kwargs") if "kwargs" in cfg_agent else None
    raw_env_type = cfg_env.get("type")
    raw_force_build = cfg_env.get("force_build")
    raw_delete = cfg_env.get("delete")
    raw_v_disable = cfg_verifier.get("disable")
    raw_v_mode = cfg_verifier.get("environment_mode")
    return _TrialMatchFields(
        task_name=str(raw_task_name) if isinstance(raw_task_name, str) and raw_task_name else None,
        task_digest=str(raw_digest) if isinstance(raw_digest, str) and raw_digest else None,
        agent_name=str(raw_agent_name) if isinstance(raw_agent_name, str) and raw_agent_name else None,
        model_name=str(raw_model_name) if isinstance(raw_model_name, str) and raw_model_name else None,
        kwargs_json=(
            canonical_json(raw_kwargs).decode("utf-8") if isinstance(raw_kwargs, dict) else None
        ),
        env_type=str(raw_env_type) if isinstance(raw_env_type, str) and raw_env_type else None,
        env_force_build=raw_force_build if type(raw_force_build) is bool else None,
        env_delete=raw_delete if type(raw_delete) is bool else None,
        verifier_disable=raw_v_disable if type(raw_v_disable) is bool else None,
        verifier_env_mode=str(raw_v_mode) if isinstance(raw_v_mode, str) and raw_v_mode else None,
    )


def merge_harbor_trial(trial_dir: Path) -> JSONDict:
    """Merge Harbor's `<trial_dir>/result.json` with StellarWeaver's `agent/trial-report.json`."""
    trial_path = Path(trial_dir)
    result_path = trial_path / "result.json"
    agent_report_path = trial_path / "agent" / "trial-report.json"
    agent_manifest_path = trial_path / "agent" / "manifest.json"
    trial_lock_path = trial_path / "lock.json"

    result_data = _read_json_dict(result_path)
    agent_report = _read_json_dict(agent_report_path)
    agent_manifest = _read_json_dict(agent_manifest_path)
    trial_lock = _read_json_dict(trial_lock_path)
    if agent_report and agent_report.get("usage_accounting_version") != 2:
        agent_report = _enrich_legacy_agent_report(
            trial_path / "agent", agent_report, confined_root=trial_path
        )

    ctrf = _parse_ctrf_summary(trial_path)
    fields = (
        _extract_fields_from_lock_entry(trial_lock)
        if trial_lock
        else _extract_fields_from_disk_trial(result_data, agent_manifest)
    )

    verdict, resolved, reward, reward_valid, exception_type = _classify_harbor_verdict(result_data)
    result_present = result_path.is_file()
    report_present = agent_report_path.is_file()
    receipt_status = "present" if result_present and report_present else "partial"
    merged: JSONDict = {
        **agent_report,
        "trial_dir": trial_path.name,
        "trial_id": result_data.get("id"),
        "trial_name": result_data.get("trial_name") or trial_path.name,
        "task_name": result_data.get("task_name") or fields.task_name,
        "task_id": result_data.get("task_id"),
        "task_digest": fields.task_digest,
        "task_checksum": result_data.get("task_checksum"),
        "planned_agent": fields.agent_name,
        "planned_model": fields.model_name,
        "verifier_result": result_data.get("verifier_result"),
        "ctrf_summary": ctrf,
        "ctrf_passed": ctrf["passed"] if ctrf is not None else None,
        "ctrf_total": ctrf["total"] if ctrf is not None else None,
        "ctrf_skipped": ctrf["skipped"] if ctrf is not None else None,
        "exception_type": exception_type,
        "exception_info": result_data.get("exception_info"),
        "reward": reward,
        "reward_valid": reward_valid,
        "verdict": verdict,
        "resolved": resolved,
        "harbor_result_present": result_present,
        "agent_report_present": report_present,
        "receipt_status": receipt_status,
    }
    if agent_manifest and "code_sha" not in merged:
        merged["code_sha"] = agent_manifest.get("code_sha")
    if "outcome" not in merged:
        agent_res = _as_dict(result_data.get("agent_result"))
        inp = int(agent_res.get("n_input_tokens") or 0)
        out = int(agent_res.get("n_output_tokens") or 0)
        merged["outcome"] = {"input_tokens": inp, "output_tokens": out}
        if "cached_input_tokens" not in merged and isinstance(agent_res.get("n_cache_tokens"), int):
            merged["cached_input_tokens"] = agent_res["n_cache_tokens"]
        if "estimated_cost_usd" not in merged and isinstance(agent_res.get("cost_usd"), (int, float)):
            merged["estimated_cost_usd"] = float(agent_res["cost_usd"])
        if inp > 0 or out > 0:
            merged = _mark_legacy_mixed_unverified(merged)
    return merged


def _build_missing_planned_report(
    *,
    planned_index: int,
    fields: _TrialMatchFields,
    job_pending_trials: int | None,
) -> JSONDict:
    return {
        "planned": True,
        "planned_index": planned_index,
        "trial_dir": None,
        "trial_id": None,
        "trial_name": None,
        "task_name": fields.task_name,
        "task_id": None,
        "task_digest": fields.task_digest,
        "planned_agent": fields.agent_name,
        "planned_model": fields.model_name,
        "verifier_result": None,
        "ctrf_summary": None,
        "ctrf_passed": None,
        "ctrf_total": None,
        "ctrf_skipped": None,
        "exception_type": None,
        "exception_info": None,
        "reward": None,
        "reward_valid": False,
        "verdict": "unscored",
        "resolved": None,
        "harbor_result_present": False,
        "agent_report_present": False,
        "receipt_status": "missing_receipt",
        "job_pending_trials": job_pending_trials,
        "outcome": {"input_tokens": 0, "output_tokens": 0},
        "usage_accounting_version": 2,
        "usage_accounting_source": "not_executed",
        "completed_model_calls": 0,
        "failed_model_calls": 0,
        "reported_input_tokens": 0,
        "reported_output_tokens": 0,
        "reported_cached_input_tokens": None,
        "reported_estimated_cost_usd": 0.0,
        "input_usage_complete": True,
        "output_usage_complete": True,
        "cache_usage_complete": True,
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        "failed_estimated_input_tokens": 0,
        "failed_estimated_output_tokens": 0,
        "failed_estimated_cost_usd": 0.0,
        "unknown_input_tokens": 0,
        "unknown_output_tokens": 0,
        "post_compaction_uncached_input_tokens": 0,
        "post_compaction_first_request_count": 0,
        "cached_input_tokens": None,
        "estimated_cost_usd": 0.0,
    }


def _task_slug(task_name: str) -> str:
    return task_name.rsplit("/", 1)[-1] if "/" in task_name else task_name


def _strict_locks_equal(
    p_entry: JSONDict,
    p_lock: Any | None,
    d_entry: JSONDict,
    d_lock: Any | None,
) -> bool:
    if p_lock is not None and d_lock is not None:
        return bool(p_lock == d_lock)
    return canonical_json(p_entry) == canonical_json(d_entry)


def _compatible_without_trial_lock(
    planned: _TrialMatchFields,
    actual: _TrialMatchFields,
    *,
    allow_slug: bool = False,
) -> bool:
    if not planned.task_name or not actual.task_name:
        return False
    if allow_slug:
        if _task_slug(planned.task_name) != _task_slug(actual.task_name):
            return False
    elif planned.task_name != actual.task_name:
        return False
    for attr in (
        "task_digest",
        "agent_name",
        "model_name",
        "kwargs_json",
        "env_type",
        "env_force_build",
        "env_delete",
        "verifier_disable",
        "verifier_env_mode",
    ):
        p_val = getattr(planned, attr)
        a_val = getattr(actual, attr)
        if p_val is not None and a_val is not None and p_val != a_val:
            return False
    return True


def _lock_entry_has_identity(entry: JSONDict) -> bool:
    task_obj = entry.get("task")
    if isinstance(task_obj, dict):
        name = task_obj.get("name")
        if isinstance(name, str) and name.strip():
            return True
    top_name = entry.get("task_name")
    return isinstance(top_name, str) and bool(top_name.strip())


def _read_lock_file_strict(
    lock_path: Path,
) -> tuple[Literal["absent", "valid", "corrupt"], list[JSONDict], str | None]:
    if not lock_path.exists():
        return "absent", [], "job root lock.json is missing"
    if not lock_path.is_file():
        return "corrupt", [], "job root lock.json is not a regular file"
    try:
        loaded = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return "corrupt", [], f"invalid JSON in lock.json ({type(exc).__name__})"
    if not isinstance(loaded, dict):
        return "corrupt", [], "lock.json root must be a JSON object"
    trials = loaded.get("trials")
    if not isinstance(trials, list) or len(trials) == 0:
        return "corrupt", [], "lock.json trials list is missing or empty"
    if any(not isinstance(item, dict) or not _lock_entry_has_identity(item) for item in trials):
        return "corrupt", [], "lock.json contains a trial entry without valid task identity"
    return "valid", list(trials), None


def _read_optional_lock_dict(lock_path: Path) -> tuple[bool, bool, JSONDict]:
    """Return (exists, corrupt, payload)."""
    if not lock_path.exists():
        return False, False, {}
    if not lock_path.is_file():
        return True, True, {}
    try:
        loaded = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return True, True, {}
    if not isinstance(loaded, dict):
        return True, True, {}
    return True, False, loaded


def collect_harbor_job_reports(job_dir: Path) -> list[JSONDict]:
    """Discover Harbor trial directories under `job_dir`, strictly anchored to `lock.json`."""
    root = Path(job_dir)
    if (root / "result.json").is_file() and (root / "agent").is_dir():
        return [merge_harbor_trial(root)]

    job_result = _read_json_dict(root / "result.json")
    stats = _as_dict(job_result.get("stats"))
    pending_raw = stats.get("n_pending_trials")
    job_pending_trials: int | None = (
        pending_raw if isinstance(pending_raw, int) and not isinstance(pending_raw, bool) else None
    )

    trial_dirs = [
        child
        for child in sorted(root.iterdir())
        if child.is_dir()
        and ((child / "result.json").is_file() or (child / "agent" / "trial-report.json").is_file())
    ]

    lock_status, planned_entries, lock_reason = _read_lock_file_strict(root / "lock.json")
    if lock_status == "corrupt":
        corrupt_reports: list[JSONDict] = []
        for trial_dir in trial_dirs:
            merged = dict(merge_harbor_trial(trial_dir))
            merged["lock_status"] = "corrupt"
            merged["lock_reason"] = lock_reason
            merged["planned"] = None
            merged["receipt_status"] = "lock_unverifiable"
            merged["job_pending_trials"] = job_pending_trials
            corrupt_reports.append(merged)
        if not corrupt_reports:
            corrupt_reports.append(
                {
                    "lock_status": "corrupt",
                    "lock_reason": lock_reason,
                    "planned": None,
                    "planned_index": None,
                    "trial_dir": None,
                    "task_name": None,
                    "verdict": "unscored",
                    "resolved": None,
                    "reward": None,
                    "reward_valid": False,
                    "receipt_status": "lock_unverifiable",
                    "job_pending_trials": job_pending_trials,
                    "outcome": {"input_tokens": 0, "output_tokens": 0},
                }
            )
        return corrupt_reports

    if lock_status == "absent":
        absent_reports: list[JSONDict] = []
        for trial_dir in trial_dirs:
            merged = dict(merge_harbor_trial(trial_dir))
            merged["lock_status"] = "absent"
            merged["lock_reason"] = lock_reason
            merged["planned"] = None
            merged["receipt_status"] = "lock_absent"
            merged["job_pending_trials"] = job_pending_trials
            absent_reports.append(merged)
        if not absent_reports:
            absent_reports.append(
                {
                    "lock_status": "absent",
                    "lock_reason": lock_reason,
                    "planned": None,
                    "planned_index": None,
                    "trial_dir": None,
                    "task_name": None,
                    "verdict": "unscored",
                    "resolved": None,
                    "reward": None,
                    "reward_valid": False,
                    "receipt_status": "lock_absent",
                    "job_pending_trials": job_pending_trials,
                    "outcome": {"input_tokens": 0, "output_tokens": 0},
                }
            )
        return absent_reports

    planned_items = [
        (entry, _harbor_trial_lock(entry), _extract_fields_from_lock_entry(entry))
        for entry in planned_entries
    ]
    disk_items: list[
        tuple[bool, bool, JSONDict, Any | None, _TrialMatchFields, JSONDict]
    ] = []
    for trial_dir in trial_dirs:
        has_lock, lock_corrupt, d_lock_dict = _read_optional_lock_dict(trial_dir / "lock.json")
        d_lock = _harbor_trial_lock(d_lock_dict) if (has_lock and not lock_corrupt) else None
        result_data = _read_json_dict(trial_dir / "result.json")
        agent_manifest = _read_json_dict(trial_dir / "agent" / "manifest.json")
        d_fields = (
            _extract_fields_from_lock_entry(d_lock_dict)
            if (has_lock and not lock_corrupt)
            else _extract_fields_from_disk_trial(result_data, agent_manifest)
        )
        disk_items.append(
            (has_lock, lock_corrupt, d_lock_dict, d_lock, d_fields, merge_harbor_trial(trial_dir))
        )

    matched_disk: list[int | None] = [None] * len(planned_items)
    used_disk: set[int] = set()

    # Pass 1: Strict TrialLock equality when both planned entry and `<trial_dir>/lock.json` exist.
    for p_idx, (p_entry, p_lock, _) in enumerate(planned_items):
        for d_idx, (has_lock, lock_corrupt, d_entry, d_lock, _, _) in enumerate(disk_items):
            if d_idx in used_disk or not has_lock or lock_corrupt:
                continue
            if _strict_locks_equal(p_entry, p_lock, d_entry, d_lock):
                matched_disk[p_idx] = d_idx
                used_disk.add(d_idx)
                break

    # Pass 2: Only when `<trial_dir>/lock.json` is absent: conflict-free & unambiguous fallback.
    for allow_slug in (False, True):
        for d_idx, (has_lock, _, _, _, d_fields, _) in enumerate(disk_items):
            if d_idx in used_disk or has_lock:
                continue
            candidates = [
                p_idx
                for p_idx, (_, _, p_fields) in enumerate(planned_items)
                if matched_disk[p_idx] is None
                and _compatible_without_trial_lock(p_fields, d_fields, allow_slug=allow_slug)
            ]
            if not candidates:
                continue
            distinct_sigs = {canonical_json(planned_items[p_idx][0]) for p_idx in candidates}
            if len(distinct_sigs) == 1:
                chosen = candidates[0]
                matched_disk[chosen] = d_idx
                used_disk.add(d_idx)

    reports: list[JSONDict] = []
    for p_idx, (_, _, p_fields) in enumerate(planned_items):
        matched_idx = matched_disk[p_idx]
        if matched_idx is not None:
            report = dict(disk_items[matched_idx][5])
            report["lock_status"] = "valid"
            report["planned"] = True
            report["planned_index"] = p_idx
            report["job_pending_trials"] = job_pending_trials
            reports.append(report)
            continue
        missing = _build_missing_planned_report(
            planned_index=p_idx,
            fields=p_fields,
            job_pending_trials=job_pending_trials,
        )
        missing["lock_status"] = "valid"
        reports.append(missing)

    for d_idx, (_, _, _, _, _, report) in enumerate(disk_items):
        if d_idx not in used_disk:
            extra = dict(report)
            extra["lock_status"] = "valid"
            extra["planned"] = False
            extra["receipt_status"] = "unplanned"
            extra["job_pending_trials"] = job_pending_trials
            reports.append(extra)
    return reports


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


def _int_or_zero(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _is_sentinel_report(item: JSONDict) -> bool:
    return (
        item.get("trial_dir") is None
        and item.get("task_name") is None
        and item.get("receipt_status") in {"lock_unverifiable", "lock_absent"}
    )


def aggregate_trial_reports(reports: Sequence[JSONDict]) -> JSONDict:
    """Aggregate planned Terminal-Bench trials cleanly, excluding `unplanned` receipts from metrics."""
    lock_corrupt = any(item.get("lock_status") == "corrupt" for item in reports)
    lock_absent = (not lock_corrupt) and any(item.get("lock_status") == "absent" for item in reports)
    lock_status: Literal["valid", "absent", "corrupt"] = (
        "corrupt" if lock_corrupt else ("absent" if lock_absent else "valid")
    )
    planned_denominator_verifiable = lock_status == "valid"
    lock_reason = next(
        (str(reason) for item in reports if isinstance(reason := item.get("lock_reason"), str) and reason),
        None,
    )
    planned_reports = [
        item for item in reports if item.get("planned") is not False and not _is_sentinel_report(item)
    ]
    unplanned_reports = [item for item in reports if item.get("planned") is False]

    pending_values = [
        val
        for item in reports
        if type(val := item.get("job_pending_trials")) is int
    ]
    job_pending_trials = pending_values[0] if pending_values else None

    total_planned = len(planned_reports) if planned_denominator_verifiable else None
    counts = {"passed": 0, "failed": 0, "timeout": 0, "unscored": 0}
    missing_receipt = 0
    observed_receipts = 0
    unexpected_prefix_divergences = 0
    for item in planned_reports:
        counts[_item_verdict(item)] += 1
        if item.get("receipt_status") == "missing_receipt":
            missing_receipt += 1
        elif item.get("receipt_status") != "not_started":
            observed_receipts += 1
            unexpected_prefix_divergences += _int_or_zero(item.get("unexpected_prefix_divergences"))

    resolved = counts["passed"]
    resolved_rate: float | None = (
        (round(resolved / total_planned, 4) if total_planned else 0.0)
        if planned_denominator_verifiable and total_planned is not None
        else None
    )
    batch_complete = (
        planned_denominator_verifiable
        and missing_receipt == 0
        and job_pending_trials in {None, 0}
    )

    total_reported_input = 0
    total_reported_output = 0
    total_reported_cached = 0
    reported_cached_trials = 0
    total_reported_cost = 0.0

    total_estimated_input = 0
    total_estimated_output = 0
    total_failed_estimated_input = 0
    total_failed_estimated_output = 0
    total_failed_estimated_cost = 0.0
    total_unknown_input = 0
    total_unknown_output = 0
    total_post_compaction_uncached = 0

    legacy_mixed_trials = 0
    total_legacy_mixed_input = 0
    total_legacy_mixed_output = 0
    total_legacy_mixed_cached = 0
    legacy_cached_complete = True
    total_legacy_mixed_cost = 0.0

    all_input_complete = True
    all_output_complete = True
    all_cache_complete = True
    all_cost_complete = True
    has_executed_verified = False

    for item in planned_reports:
        if item.get("receipt_status") in {"missing_receipt", "not_started"}:
            continue
        is_legacy_mixed = item.get("usage_accounting_source") == "legacy_mixed_unverified" or (
            item.get("usage_accounting_version") != 2
            and item.get("reported_input_tokens") is None
            and "input_usage_complete" not in item
        )
        if is_legacy_mixed:
            legacy_mixed_trials += 1
            all_input_complete = False
            all_output_complete = False
            all_cache_complete = False
            all_cost_complete = False
            outcome = _as_dict(item.get("outcome"))
            leg_in = _int_or_zero(item.get("legacy_mixed_input_tokens", outcome.get("input_tokens")))
            leg_out = _int_or_zero(
                item.get("legacy_mixed_output_tokens", outcome.get("output_tokens"))
            )
            leg_cached = item.get(
                "legacy_mixed_cached_input_tokens", item.get("cached_input_tokens")
            )
            leg_cost = _float_or_none(
                item.get("legacy_mixed_cost_usd", item.get("estimated_cost_usd"))
            )
            total_legacy_mixed_input += leg_in
            total_legacy_mixed_output += leg_out
            if type(leg_cached) is int:
                total_legacy_mixed_cached += leg_cached
            elif leg_in > 0:
                legacy_cached_complete = False
            if leg_cost is not None:
                total_legacy_mixed_cost += leg_cost
            continue

        has_executed_verified = True
        total_reported_input += _int_or_zero(item.get("reported_input_tokens"))
        total_reported_output += _int_or_zero(item.get("reported_output_tokens"))
        if type(rep_cached := item.get("reported_cached_input_tokens")) is int:
            total_reported_cached += rep_cached
            reported_cached_trials += 1

        total_estimated_input += _int_or_zero(item.get("estimated_input_tokens"))
        total_estimated_output += _int_or_zero(item.get("estimated_output_tokens"))
        total_failed_estimated_input += _int_or_zero(item.get("failed_estimated_input_tokens"))
        total_failed_estimated_output += _int_or_zero(item.get("failed_estimated_output_tokens"))
        if (f_cost := _float_or_none(item.get("failed_estimated_cost_usd"))) is not None:
            total_failed_estimated_cost += f_cost
        total_unknown_input += _int_or_zero(item.get("unknown_input_tokens"))
        total_unknown_output += _int_or_zero(item.get("unknown_output_tokens"))
        total_post_compaction_uncached += _int_or_zero(
            item.get("post_compaction_uncached_input_tokens")
        )

        if item.get("input_usage_complete") is False:
            all_input_complete = False
        if item.get("output_usage_complete") is False:
            all_output_complete = False
        if item.get("cache_usage_complete") is False:
            all_cache_complete = False
        if (rep_cost := _float_or_none(item.get("reported_estimated_cost_usd"))) is not None:
            total_reported_cost += rep_cost
        else:
            all_cost_complete = False

    reported_cached_out: int | None = (
        total_reported_cached if (all_cache_complete and reported_cached_trials > 0) else None
    )
    reported_cost_out: float | None = (
        round(total_reported_cost, 4) if (all_cost_complete and has_executed_verified) else (
            0.0 if not has_executed_verified and legacy_mixed_trials == 0 else None
        )
    )
    cache_ratio: float | None = (
        round(total_reported_cached / total_reported_input, 4)
        if all_cache_complete and reported_cached_trials > 0 and total_reported_input > 0
        else None
    )

    # Legacy fallback aliases ONLY when pure legacy unverified archive has 0 v2 reported trials.
    legacy_only = legacy_mixed_trials > 0 and not has_executed_verified
    alias_input = total_legacy_mixed_input if legacy_only else total_reported_input
    alias_output = total_legacy_mixed_output if legacy_only else total_reported_output
    alias_cached: int | None = (
        (total_legacy_mixed_cached if legacy_cached_complete else None)
        if legacy_only
        else reported_cached_out
    )
    alias_cache_ratio = (
        round(alias_cached / alias_input, 4)
        if legacy_only and alias_cached is not None and alias_input > 0
        else cache_ratio
    )
    alias_cost: float | None = (
        round(total_legacy_mixed_cost, 4) if legacy_only else reported_cost_out
    )

    return {
        "tasks": total_planned,
        "observed_receipts": observed_receipts,
        "batch_complete": batch_complete,
        "planned_denominator_verifiable": planned_denominator_verifiable,
        "lock_status": lock_status,
        "lock_reason": lock_reason,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "timeout": counts["timeout"],
        "unscored": counts["unscored"],
        "missing_receipt": missing_receipt,
        "unplanned": len(unplanned_reports),
        "job_pending_trials": job_pending_trials,
        "resolved": resolved,
        "resolved_rate": resolved_rate,
        "unexpected_prefix_divergences": unexpected_prefix_divergences,
        "input_usage_complete": all_input_complete and has_executed_verified,
        "output_usage_complete": all_output_complete and has_executed_verified,
        "cache_usage_complete": all_cache_complete and reported_cached_trials > 0,
        "total_reported_input_tokens": total_reported_input,
        "total_reported_cached_input_tokens": reported_cached_out,
        "total_reported_output_tokens": total_reported_output,
        "total_reported_estimated_cost_usd": reported_cost_out,
        "total_estimated_input_tokens": total_estimated_input,
        "total_estimated_output_tokens": total_estimated_output,
        "total_failed_estimated_input_tokens": total_failed_estimated_input,
        "total_failed_estimated_output_tokens": total_failed_estimated_output,
        "total_failed_estimated_cost_usd": round(total_failed_estimated_cost, 4),
        "total_unknown_input_tokens": total_unknown_input,
        "total_unknown_output_tokens": total_unknown_output,
        "total_post_compaction_uncached_input_tokens": total_post_compaction_uncached,
        "legacy_mixed_trials": legacy_mixed_trials,
        "total_legacy_mixed_input_tokens": total_legacy_mixed_input,
        "total_legacy_mixed_output_tokens": total_legacy_mixed_output,
        "total_legacy_mixed_cached_input_tokens": (
            total_legacy_mixed_cached if legacy_mixed_trials > 0 and legacy_cached_complete else None
        ),
        "total_legacy_mixed_cost_usd": round(total_legacy_mixed_cost, 4),
        "total_input_tokens": alias_input,
        "total_cached_input_tokens": alias_cached,
        "cache_hit_ratio": alias_cache_ratio,
        "total_output_tokens": alias_output,
        "total_estimated_cost_usd": alias_cost,
    }


def render_markdown_summary(reports: Sequence[JSONDict], *, agg: JSONDict | None = None) -> str:
    """Render a compact Markdown summary table compatible with docs/TerminalBench4.1Eval.md."""
    summary = agg if agg is not None else aggregate_trial_reports(reports)
    cached_val = summary["total_reported_cached_input_tokens"]
    if (
        cached_val is None
        and summary["legacy_mixed_trials"] > 0
        and summary["total_reported_input_tokens"] == 0
    ):
        cached_val = summary["total_cached_input_tokens"]
    cached_text = str(cached_val) if cached_val is not None else "incomplete"
    ratio_val = summary["cache_hit_ratio"]
    ratio_text = f"{ratio_val * 100:.2f}%" if ratio_val is not None else "incomplete"
    lock_status = summary.get("lock_status", "valid")
    if lock_status == "corrupt":
        denominator_label = "unverifiable (corrupt lock.json)"
        resolved_line = f"{summary['passed']} (unverifiable)"
    elif lock_status == "absent":
        denominator_label = "unverifiable (missing lock.json)"
        resolved_line = f"{summary['passed']} observed (rate unknown; missing lock.json)"
    else:
        denominator_label = "valid"
        resolved_line = (
            f"{summary['passed']} (unverifiable)"
            if summary["resolved_rate"] is None
            else f"{summary['passed']} ({summary['resolved_rate'] * 100:.1f}%)"
        )
    tasks_text = "unknown" if summary["tasks"] is None else str(summary["tasks"])
    observed_text = (
        f"{summary['observed_receipts']} / {summary['tasks']}"
        if summary["tasks"] is not None
        else str(summary["observed_receipts"])
    )
    reported_cost_text = (
        f"${summary['total_reported_estimated_cost_usd']:.4f}"
        if summary["total_reported_estimated_cost_usd"] is not None
        else "incomplete"
    )
    lines = [
        "| Metric | Value |",
        "| --- | --- |",
        f"| Planned Denominator | {denominator_label} |",
    ]
    if summary.get("lock_reason"):
        lines.append(f"| Lock Reason | {summary['lock_reason']} |")
    lines.extend(
        [
            f"| Tasks | {tasks_text} |",
            f"| Observed Receipts | {observed_text} |",
            f"| Batch Complete | {'yes' if summary['batch_complete'] else 'no'} |",
            f"| Resolved (Passed) | {resolved_line} |",
            f"| Failed | {summary['failed']} |",
            f"| Timeout | {summary['timeout']} |",
            f"| Unscored | {summary['unscored']} |",
            f"| Missing Receipt | {summary['missing_receipt']} |",
            f"| Unplanned | {summary['unplanned']} |",
        ]
    )
    if summary["job_pending_trials"] is not None:
        lines.append(f"| Job Pending Trials | {summary['job_pending_trials']} |")
    if summary.get("unexpected_prefix_divergences"):
        lines.append(
            f"| Unexpected Prefix Divergences | {summary['unexpected_prefix_divergences']} |"
        )
    lines.extend(
        [
            f"| Reported Input Tokens | {summary['total_reported_input_tokens']:,} |",
            f"| Cached Input Tokens | {cached_text} |",
            f"| Cache Hit Ratio | {ratio_text} |",
            f"| Reported Output Tokens | {summary['total_reported_output_tokens']:,} |",
            f"| Reported Estimated Cost (USD) | {reported_cost_text} |",
            f"| Failed Estimated Input Tokens | {summary['total_failed_estimated_input_tokens']:,} |",
            f"| Failed Estimated Output Tokens | {summary['total_failed_estimated_output_tokens']:,} |",
            f"| Failed Estimated Cost (USD) | ${summary['total_failed_estimated_cost_usd']:.4f} |",
        ]
    )
    if summary["total_unknown_input_tokens"] > 0:
        lines.append(f"| Unknown Input Tokens | {summary['total_unknown_input_tokens']:,} |")
    if summary["total_unknown_output_tokens"] > 0:
        lines.append(f"| Unknown Output Tokens | {summary['total_unknown_output_tokens']:,} |")
    if summary["legacy_mixed_trials"] > 0:
        lines.append(f"| Legacy Mixed Trials | {summary['legacy_mixed_trials']} |")
        lines.append(
            f"| Legacy Mixed Input Tokens | {summary['total_legacy_mixed_input_tokens']:,} |"
        )
        lines.append(
            f"| Legacy Mixed Output Tokens | {summary['total_legacy_mixed_output_tokens']:,} |"
        )
        lines.append(f"| Legacy Mixed Cost (USD) | ${summary['total_legacy_mixed_cost_usd']:.4f} |")
    if summary["total_post_compaction_uncached_input_tokens"] > 0:
        lines.append(
            f"| Post-Compaction Uncached Input Tokens | {summary['total_post_compaction_uncached_input_tokens']:,} |"
        )
    return "\n".join(lines) + "\n"


def summarize_harbor_job(job_dir: Path) -> JSONDict:
    """Collect, aggregate, and render a Harbor job directory from one unified root-lock summary."""
    reports = collect_harbor_job_reports(job_dir)
    summary = aggregate_trial_reports(reports)
    return {
        "job_dir": str(job_dir),
        "lock_status": summary["lock_status"],
        "lock_reason": summary["lock_reason"],
        "planned_denominator_verifiable": summary["planned_denominator_verifiable"],
        "batch_complete": summary["batch_complete"],
        "summary": summary,
        "markdown": render_markdown_summary(reports, agg=summary),
        "trials": reports,
    }


FAST19_RESOURCE_MANIFEST: dict[str, JSONDict] = {
    "terminal-bench/atrx-vep-crispr": {
        "digest": "sha256:81050619444198001d1b40ff2415d4043283433c8d4efd70e8e160af99f57620",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/cad-model": {
        "digest": "sha256:75264fb8e309eda38c1f25c31d8757385caa944a9a44252d7ec59d448f5e7eae",
        "agent_memory_mb": 8192,
        "verifier_memory_mb": 8192,
        "verifier_mode": "separate",
    },
    "terminal-bench/cargo-flight-dispatch": {
        "digest": "sha256:a5ee048cbb791bbebc7e71652f26faf4438bf86c9ca2f231a50ba859a7e423ed",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/fin-saccr-rwa": {
        "digest": "sha256:38095e449bc3eccc2bdc0b61c0899f0d6dad547d3705951c054d9d65ba732e74",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/gsea-proteomics": {
        "digest": "sha256:e35f61b7e319fa7eaabaf52636dd2ec0b102bd16e69a13665d1c19d92b7b510e",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/hof-topology-interpenetration": {
        "digest": "sha256:46353b47672f2ac6c7e3cbf66a9cddc78c92d63e6bf4c5ffe1baca67c1c08bd0",
        "agent_memory_mb": 8192,
        "verifier_memory_mb": 8192,
        "verifier_mode": "separate",
    },
    "terminal-bench/html-js-filter": {
        "digest": "sha256:7dbe36e64d4fdf1d8206bad74aa3fffefd66a01b3508b983bc8156f09d642ff1",
        "agent_memory_mb": 8192,
        "verifier_memory_mb": 8192,
        "verifier_mode": "separate",
    },
    "terminal-bench/mvcc-lsm-compaction": {
        "digest": "sha256:a72c20ba467d6cd8e855ca0cfb00512dc53464ea02d26042e9eb4adc1cb276ce",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/photonic-waveguide-routing": {
        "digest": "sha256:a4801300795de243b36566833ed1f6ea5bbbbeb9645db0c9ed91dde36bb9444c",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/production-planning": {
        "digest": "sha256:7f7237a15a5a8a677a9ddee8d13e5c1576839f3a06202c45f745d1ffada447da",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/react-lead-form": {
        "digest": "sha256:1127f3bf27b4b15441623eb07a28e4f4007f8115f8764e4289d10f1bcd894779",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/satb-audio-transcription": {
        "digest": "sha256:134c71221d0ee432bd8c9235242f4727ca55a1cc960adb34538dd8f07f2e9adc",
        "agent_memory_mb": 8192,
        "verifier_memory_mb": 8192,
        "verifier_mode": "separate",
    },
    "terminal-bench/session-window-debug": {
        "digest": "sha256:2f1a6389c24ab58d0ca4765031fe7c8799ba31284f0af9c7fcd203ad652bfa45",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/sound-change-cascade": {
        "digest": "sha256:65d43e3fe13768e8983f31bf7b9d265d1a15c4d6a721e5cff469c39cbebf2367",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/telecom-entity-resolution": {
        "digest": "sha256:fd037ff5d13efd33dceb4f23f621122ff1f1b0dda8fe9d75dfe5e3164a9e7e20",
        "agent_memory_mb": 8192,
        "verifier_memory_mb": 8192,
        "verifier_mode": "separate",
    },
    "terminal-bench/vba-userform-port": {
        "digest": "sha256:bb2c3e8769cb334b52b69b0cb0dcf964e10cf13eb1d0c2f15d74e407e62be651",
        "agent_memory_mb": 8192,
        "verifier_memory_mb": 8192,
        "verifier_mode": "separate",
    },
    "terminal-bench/vllm-deepseek-streaming": {
        "digest": "sha256:5b64c30e3d43e6b138351df02858d09f795f6af4cc48f80d25113fe46f7c499b",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
    "terminal-bench/vpp-loss-divergence": {
        "digest": "sha256:8c4071ec1b7ff23009b572515d7eb4f10216d3b5c8b9c3c955f9274413b16109",
        "agent_memory_mb": 8192,
        "verifier_memory_mb": 8192,
        "verifier_mode": "separate",
    },
    "terminal-bench/wal-recovery-ordering": {
        "digest": "sha256:79ba48ef2fcf2a9f06243b66ae332eafa93b929a2475a2c6203e71708b41c6e5",
        "agent_memory_mb": 4096,
        "verifier_memory_mb": 4096,
        "verifier_mode": "separate",
    },
}


def _normalize_digest(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().removeprefix("sha256:")
    return f"sha256:{cleaned}" if cleaned else None


def get_docker_mem_total_bytes() -> int:
    """Query local Docker engine MemTotal in bytes via `docker info`."""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except OSError as exc:
        raise RuntimeError(f"Docker CLI unavailable: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"docker info failed (exit {proc.returncode}): {detail}")
    raw = proc.stdout.strip()
    try:
        mem_bytes = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Unexpected MemTotal output from docker info: {raw!r}") from exc
    if mem_bytes <= 0:
        raise RuntimeError(f"Docker MemTotal must be positive, got {mem_bytes}")
    return mem_bytes


def _extract_toml_memory_spec(toml_path: Path, *, digest_hex: str) -> JSONDict:
    parsed = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    env_cfg = _as_dict(parsed.get("environment"))
    ver_cfg = _as_dict(parsed.get("verifier"))
    ver_env = _as_dict(ver_cfg.get("environment"))
    ver_mode = str(ver_cfg.get("environment_mode") or "shared")
    agent_mb = env_cfg.get("memory_mb")
    verifier_mb = ver_env.get("memory_mb") if ver_mode == "separate" else agent_mb
    return {
        "status": "verified",
        "source": "local_cache",
        "digest": f"sha256:{digest_hex}",
        "agent_memory_mb": int(agent_mb) if type(agent_mb) is int else None,
        "verifier_memory_mb": int(verifier_mb) if type(verifier_mb) is int else None,
        "verifier_mode": ver_mode,
        "toml_path": str(toml_path),
    }


def resolve_task_memory_spec(
    task_name: str,
    *,
    expected_digest: str | None = None,
    cache_root: Path | None = None,
    manifest: Mapping[str, JSONDict] | None = None,
) -> JSONDict:
    """Resolve Agent & Verifier memory requirements from local Harbor cache or digest-verified manifest."""
    normalized_expected = _normalize_digest(expected_digest)
    cache_base = (
        cache_root
        if cache_root is not None
        else Path.home() / ".cache" / "harbor" / "tasks" / "packages"
    )
    parts = [part for part in task_name.strip().split("/") if part]
    if len(parts) == 2:
        org, slug = parts
        task_cache_dir = cache_base / org / slug
        if task_cache_dir.is_dir():
            if normalized_expected is not None:
                digest_hex = normalized_expected.removeprefix("sha256:")
                candidate = task_cache_dir / digest_hex / "task.toml"
                if candidate.is_file():
                    return _extract_toml_memory_spec(candidate, digest_hex=digest_hex)
            else:
                cached_tomls = sorted(task_cache_dir.glob("*/task.toml"))
                if cached_tomls:
                    latest = cached_tomls[-1]
                    return _extract_toml_memory_spec(latest, digest_hex=latest.parent.name)

    resource_manifest = FAST19_RESOURCE_MANIFEST if manifest is None else manifest
    entry = resource_manifest.get(task_name)
    if entry is not None:
        manifest_digest = _normalize_digest(
            str(entry["digest"]) if entry.get("digest") is not None else None
        )
        if (
            normalized_expected is not None
            and manifest_digest is not None
            and normalized_expected != manifest_digest
        ):
            return {
                "status": "unverified",
                "reason": "digest_mismatch",
                "expected_digest": normalized_expected,
                "manifest_digest": manifest_digest,
                "agent_memory_mb": None,
                "verifier_memory_mb": None,
                "verifier_mode": None,
            }
        agent_mb = entry.get("agent_memory_mb")
        ver_mb = entry.get("verifier_memory_mb")
        return {
            "status": "verified",
            "source": "pinned_manifest",
            "digest": manifest_digest,
            "agent_memory_mb": int(agent_mb) if type(agent_mb) is int else None,
            "verifier_memory_mb": int(ver_mb) if type(ver_mb) is int else None,
            "verifier_mode": str(entry.get("verifier_mode") or "separate"),
        }

    return {
        "status": "unverified",
        "reason": "metadata_unavailable",
        "expected_digest": normalized_expected,
        "agent_memory_mb": None,
        "verifier_memory_mb": None,
        "verifier_mode": None,
    }


def _load_yaml_dict(config_path: Path) -> JSONDict:
    yaml_mod = importlib.import_module("yaml")
    loaded = yaml_mod.safe_load(config_path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _extract_selected_tasks(job_cfg: JSONDict) -> list[tuple[str, str | None]]:
    selected: list[tuple[str, str | None]] = []
    for ds in job_cfg.get("datasets") or []:
        if not isinstance(ds, dict):
            continue
        for raw_name in ds.get("task_names") or []:
            if isinstance(raw_name, str) and raw_name.strip():
                selected.append((raw_name.strip(), None))
    for task_entry in job_cfg.get("tasks") or []:
        if not isinstance(task_entry, dict):
            continue
        name = task_entry.get("name")
        digest_val = task_entry.get("digest")
        if isinstance(name, str) and name.strip():
            selected.append(
                (name.strip(), str(digest_val) if isinstance(digest_val, str) else None)
            )
    return selected


def run_docker_memory_preflight(
    config_path: Path,
    *,
    docker_mem_bytes_fn: Callable[[], int] = get_docker_mem_total_bytes,
    cache_root: Path | None = None,
    manifest: Mapping[str, JSONDict] | None = None,
    task_digests: Mapping[str, str] | None = None,
    headroom_warning_mb: int = 1024,
) -> JSONDict:
    """Validate Docker VM MemTotal against every selected task's Agent & Verifier memory requirements."""
    cfg_path = Path(config_path)
    job_cfg = _load_yaml_dict(cfg_path)
    env_cfg = _as_dict(job_cfg.get("environment"))
    env_type = str(env_cfg.get("type") or "docker")
    n_concurrent = int(job_cfg.get("n_concurrent_trials") or 1)
    selected_tasks = _extract_selected_tasks(job_cfg)

    if env_type != "docker":
        return {
            "ok": True,
            "skipped": True,
            "reason": f"environment.type={env_type!r} does not use local Docker",
            "config_path": str(cfg_path),
            "tasks_checked": len(selected_tasks),
            "docker_reachable": None,
            "docker_mem_bytes": None,
            "docker_mem_gib": None,
            "n_concurrent_trials": n_concurrent,
            "exceeded": [],
            "unverified": [],
            "warnings": [],
        }

    try:
        docker_mem_bytes = docker_mem_bytes_fn()
    except Exception as exc:
        return {
            "ok": False,
            "skipped": False,
            "config_path": str(cfg_path),
            "tasks_checked": len(selected_tasks),
            "docker_reachable": False,
            "error": str(exc),
            "docker_mem_bytes": None,
            "docker_mem_gib": None,
            "n_concurrent_trials": n_concurrent,
            "exceeded": [],
            "unverified": [],
            "warnings": [],
        }

    available_mb = round(docker_mem_bytes / (1024 * 1024), 1)
    docker_mem_gib = round(docker_mem_bytes / (1024**3), 2)
    exceeded: list[JSONDict] = []
    unverified: list[JSONDict] = []
    warnings: list[str] = []
    max_required_mb = 0

    for task_name, inline_digest in selected_tasks:
        expected_digest = (
            task_digests.get(task_name) if task_digests is not None else inline_digest
        )
        spec = resolve_task_memory_spec(
            task_name,
            expected_digest=expected_digest,
            cache_root=cache_root,
            manifest=manifest,
        )
        if spec.get("status") != "verified":
            unverified.append(
                {
                    "task": task_name,
                    "reason": spec.get("reason", "unknown"),
                }
            )
            continue
        agent_mb = spec.get("agent_memory_mb")
        ver_mb = spec.get("verifier_memory_mb")
        sides: list[str] = []
        side_reqs: list[int] = []
        if type(agent_mb) is int:
            side_reqs.append(agent_mb)
            if agent_mb * 1024 * 1024 > docker_mem_bytes:
                sides.append("agent")
        if type(ver_mb) is int:
            side_reqs.append(ver_mb)
            if ver_mb * 1024 * 1024 > docker_mem_bytes:
                sides.append("verifier")
        task_max_mb = max(side_reqs) if side_reqs else 0
        if task_max_mb > max_required_mb:
            max_required_mb = task_max_mb
        if sides:
            exceeded.append(
                {
                    "task": task_name,
                    "sides": sides,
                    "agent_memory_mb": agent_mb,
                    "verifier_memory_mb": ver_mb,
                    "required_mb": task_max_mb,
                    "available_mb": available_mb,
                    "source": spec.get("source"),
                }
            )

    if not exceeded and max_required_mb > 0:
        headroom_mb = available_mb - max_required_mb
        if headroom_mb < headroom_warning_mb:
            warnings.append(
                f"Low Docker memory headroom: peak container {max_required_mb} MiB vs Docker VM "
                f"{available_mb:.1f} MiB (headroom {headroom_mb:.1f} MiB < {headroom_warning_mb} MiB)."
            )
        if n_concurrent > 1 and n_concurrent * max_required_mb * 1024 * 1024 > docker_mem_bytes:
            warnings.append(
                f"Concurrency warning: n_concurrent_trials={n_concurrent} with {max_required_mb} MiB "
                f"tasks can demand {n_concurrent * max_required_mb} MiB > Docker VM {available_mb:.1f} MiB."
            )

    return {
        "ok": len(exceeded) == 0,
        "skipped": False,
        "config_path": str(cfg_path),
        "tasks_checked": len(selected_tasks),
        "docker_reachable": True,
        "docker_mem_bytes": docker_mem_bytes,
        "docker_mem_gib": docker_mem_gib,
        "docker_mem_mb": available_mb,
        "max_required_mb": max_required_mb,
        "n_concurrent_trials": n_concurrent,
        "exceeded": exceeded,
        "unverified": unverified,
        "warnings": warnings,
    }


def format_preflight_report(report: JSONDict) -> str:
    if report.get("skipped"):
        return f"Preflight skipped: {report.get('reason')}"
    if not report.get("docker_reachable"):
        return f"PREFLIGHT FAILED: Docker unreachable ({report.get('error')})"
    lines = [
        (
            f"Docker VM Memory: {report['docker_mem_gib']} GiB "
            f"({report['docker_mem_mb']} MiB) | Tasks checked: {report['tasks_checked']} | "
            f"Peak container spec: {report['max_required_mb']} MiB | "
            f"n_concurrent_trials: {report['n_concurrent_trials']}"
        )
    ]
    exceeded = report.get("exceeded") or []
    if exceeded:
        lines.append(
            f"PREFLIGHT FAILED: {len(exceeded)} task(s) exceed available Docker VM memory:"
        )
        for item in exceeded:
            lines.append(
                f"  - {item['task']} ({', '.join(item['sides'])}): requires {item['required_mb']} MiB > "
                f"Docker {item['available_mb']} MiB [{item['source']}]"
            )
    else:
        lines.append("PREFLIGHT PASSED: every verified task fits inside Docker VM memory.")
    for item in report.get("unverified") or []:
        lines.append(f"WARNING [unverified task metadata]: {item['task']} ({item['reason']})")
    for warn in report.get("warnings") or []:
        lines.append(f"WARNING: {warn}")
    return "\n".join(lines)


def _resolve_harbor_executable(harbor_bin: str | None = None) -> str:
    if harbor_bin:
        return harbor_bin
    sibling = Path(sys.executable).resolve().with_name(
        "harbor.exe" if os.name == "nt" else "harbor"
    )
    if sibling.is_file():
        return str(sibling)
    found = shutil.which("harbor")
    return found or str(sibling)


def launch_harbor_with_preflight(
    config_path: Path,
    *,
    env_file: Path | None = None,
    harbor_bin: str | None = None,
    extra_args: Sequence[str] = (),
    preflight_fn: Callable[[Path], JSONDict] = run_docker_memory_preflight,
    runner_fn: Callable[..., Any] = subprocess.run,
) -> int:
    """Run Docker memory preflight before invoking Harbor; abort with code 2 if preflight fails."""
    report = preflight_fn(Path(config_path))
    print(format_preflight_report(report))
    if not report.get("ok"):
        return 2
    cmd = [_resolve_harbor_executable(harbor_bin), "run", "--config", str(config_path)]
    if env_file is not None:
        cmd.extend(["--env-file", str(env_file)])
    cmd.extend(extra_args)
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    completed = runner_fn(cmd, env=env, check=False)
    return int(getattr(completed, "returncode", 0))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m stellarweaver.evals.terminal_bench",
        description="Terminal-Bench 4.1 Harbor preflight, launcher, and root-lock summarizer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="Check Docker VM memory against selected task specs.")
    pre.add_argument("--config", required=True, type=Path, help="Harbor YAML configuration path.")
    pre.add_argument("--json", action="store_true", help="Emit structured JSON report.")

    run_p = sub.add_parser("run", help="Run Docker memory preflight, then launch Harbor.")
    run_p.add_argument("--config", required=True, type=Path, help="Harbor YAML configuration path.")
    run_p.add_argument("--env-file", type=Path, default=None, help="Optional .env file path.")
    run_p.add_argument("--harbor-bin", default=None, help="Optional path to harbor executable.")
    run_p.add_argument("extra_args", nargs="*", help="Additional arguments passed to harbor run.")

    sum_p = sub.add_parser("summarize", help="Summarize a Harbor job directory.")
    sum_p.add_argument("job_dir", type=Path, help="Harbor job directory containing lock.json / trials.")
    sum_p.add_argument("--json", action="store_true", help="Emit unified JSON summary & trials.")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "preflight":
        report = run_docker_memory_preflight(args.config)
        print(
            json.dumps(report, indent=2, ensure_ascii=False)
            if args.json
            else format_preflight_report(report)
        )
        return 0 if report.get("ok") else 2
    if args.command == "run":
        return launch_harbor_with_preflight(
            args.config,
            env_file=args.env_file,
            harbor_bin=args.harbor_bin,
            extra_args=args.extra_args,
        )
    if args.command == "summarize":
        summary = summarize_harbor_job(args.job_dir)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(summary["markdown"], end="")
        return 0 if summary["planned_denominator_verifiable"] else 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
