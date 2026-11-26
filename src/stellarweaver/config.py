"""Strict configuration, with API credentials kept outside serializable settings."""

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator

from stellarweaver.domain import DEFAULT_CONTEXT_WINDOW_TOKENS


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _default_output_tokens(data: dict[str, object]) -> int:
    if data.get("provider") == "deepseek" and data.get("thinking") == "enabled":
        return 16384
    return 4096


def _default_request_timeout(data: dict[str, object]) -> int:
    if data.get("provider") == "deepseek" and data.get("thinking") == "enabled":
        return 240
    return 90


class ModelConfig(StrictConfig):
    provider: Literal["fake", "deepseek", "openai-compatible-chat"] = "fake"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "STELLARWEAVER_API_KEY"
    thinking: Literal["enabled", "disabled"] = "enabled"
    reasoning_effort: Literal["low", "high", "max"] = "high"
    context_window_tokens: int = Field(default=DEFAULT_CONTEXT_WINDOW_TOKENS, ge=2048)
    max_output_tokens: int = Field(default_factory=_default_output_tokens, ge=1)
    request_timeout_seconds: int = Field(default_factory=_default_request_timeout, ge=1, le=900)
    supports_streaming: bool = False
    supports_tools: bool = True

    @model_validator(mode="after")
    def output_fits_context(self) -> "ModelConfig":
        if self.max_output_tokens >= self.context_window_tokens:
            raise ValueError("max_output_tokens must be smaller than context_window_tokens")
        if not self.base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError("base_url must use HTTPS (HTTP is allowed for local testing)")
        return self


class LimitsConfig(StrictConfig):
    max_model_calls: int | None = Field(default=30, ge=1)
    max_tool_calls: int = Field(default=100, ge=1)
    max_wall_seconds: int = Field(default=900, ge=1)
    max_input_tokens_total: int = Field(default=1_000_000, ge=1)
    max_output_tokens_total: int = Field(default=30000, ge=1)


class ExecutionConfig(StrictConfig):
    backend: Literal["host", "docker"] = "host"
    image: str | None = None
    profile: Literal["sandbox-workspace", "sandbox-read-only"] = "sandbox-workspace"
    allow_argv: bool = False
    default_timeout_seconds: int = Field(default=60, ge=1, le=1800)
    max_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    max_output_bytes_per_stream: int = Field(default=1048576, ge=1024)

    @model_validator(mode="after")
    def timeout_order(self) -> "ExecutionConfig":
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("default_timeout_seconds must not exceed max_timeout_seconds")
        if self.backend == "docker" and (
            self.image is None
            or not re.fullmatch(r"(?:[a-z0-9._:/-]+@)?sha256:[a-f0-9]{64}", self.image)
        ):
            raise ValueError("Docker execution requires a pinned image ID or repository digest")
        return self


PolicyPreset = Literal["ask", "read-only", "workspace-edit", "auto", "benchmark"]


class PolicyConfig(StrictConfig):
    preset: PolicyPreset = "ask"


class ContextConfig(StrictConfig):
    trigger_ratio: float = Field(default=0.80, gt=0, lt=1)
    target_ratio: float = Field(default=0.55, gt=0, lt=1)
    reserve_tokens: int = Field(default=2048, ge=0)
    recent_complete_rounds: int = Field(default=6, ge=1)
    fresh_tool_rounds: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def threshold_order(self) -> "ContextConfig":
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("target_ratio must be smaller than trigger_ratio")
        return self


class RetryConfig(StrictConfig):
    max_retries: int = Field(default=2, ge=0, le=8)
    base_delay_seconds: float = Field(default=0.5, ge=0, le=30)
    max_delay_seconds: float = Field(default=30.0, gt=0, le=120)


HarnessProfile = Literal["custom", "swe-python", "terminal-minimal"]


class HarnessConfig(StrictConfig):
    profile: HarnessProfile = "custom"
    development_tools: bool = False
    issue_navigation: bool = False
    repair_controller: Literal["off", "feedback", "enforce"] = "off"
    patch_staging: bool = False
    task_evidence: bool = False
    budget_guidance: bool = False
    pipeline_metrics: bool = False
    multi_edit_tools: bool = False
    post_edit_syntax_hook: bool = False
    smart_tool_truncation: bool = False
    navigation_prefilter: bool = False
    multi_file_apply: bool = False
    parallel_read_tools: bool = False
    scoped_test_grants: bool = False
    incremental_sandbox_snapshots: bool = False
    import_aware_repomap: bool = False
    subagent_exploration: bool = False
    exploration_max_model_calls: int = Field(default=20, ge=1)
    exploration_max_tool_calls: int = Field(default=60, ge=1)
    exploration_timeout_seconds: int = Field(default=300, ge=1)
    exploration_stall_rounds: int = Field(default=3, ge=2)
    calibrated_context_budget: bool = False
    sticky_context_checkpoint: bool = False
    protect_fresh_tool_output: bool = False
    include_linter: bool = True
    soft_test_failure_reminder: bool = False
    completion_evidence_reminder: bool = False
    max_length_continuations: int = Field(default=0, ge=0, le=10)
    repeated_failure_soft_threshold: int = Field(default=3, ge=1, le=20)
    repeated_failure_hard_threshold: int = Field(default=3, ge=1, le=20)
    benchmark_allow_denied_tool_continue: bool = False
    stream_repetition_detection: bool = False
    stream_repetition_max_retries: int = Field(default=2, ge=0, le=5)

    @model_validator(mode="after")
    def failure_threshold_order(self) -> "HarnessConfig":
        if self.repeated_failure_hard_threshold < self.repeated_failure_soft_threshold:
            raise ValueError(
                "repeated_failure_hard_threshold must not be smaller than repeated_failure_soft_threshold"
            )
        return self


class AppConfig(StrictConfig):
    model: ModelConfig = Field(default_factory=ModelConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    harness: HarnessConfig = Field(default_factory=HarnessConfig)


def swe_python_harness(*, include_linter: bool = False, **overrides: Any) -> HarnessConfig:
    """Named profile for Python repository benchmarks (SWE-bench / SWE-bench Pro)."""
    defaults: dict[str, Any] = {
        "profile": "swe-python",
        "development_tools": True,
        "issue_navigation": True,
        "repair_controller": "feedback",
        "patch_staging": True,
        "task_evidence": True,
        "budget_guidance": True,
        "pipeline_metrics": True,
        "multi_edit_tools": True,
        "post_edit_syntax_hook": True,
        "smart_tool_truncation": True,
        "navigation_prefilter": True,
        "multi_file_apply": True,
        "parallel_read_tools": True,
        "scoped_test_grants": True,
        "incremental_sandbox_snapshots": True,
        "import_aware_repomap": True,
        "subagent_exploration": True,
        "calibrated_context_budget": True,
        "sticky_context_checkpoint": True,
        "protect_fresh_tool_output": True,
        "include_linter": include_linter,
        "soft_test_failure_reminder": True,
        "max_length_continuations": 3,
        "repeated_failure_soft_threshold": 3,
        "repeated_failure_hard_threshold": 6,
        "benchmark_allow_denied_tool_continue": True,
        "stream_repetition_detection": True,
        "stream_repetition_max_retries": 2,
    }
    defaults.update(overrides)
    return HarnessConfig.model_validate(defaults)


def terminal_minimal_harness(**overrides: Any) -> HarnessConfig:
    """Named profile for isolated persistent containers (Harbor / Terminal-Bench 4.1)."""
    defaults: dict[str, Any] = {
        "profile": "terminal-minimal",
        "development_tools": False,
        "issue_navigation": False,
        "repair_controller": "feedback",
        "patch_staging": False,
        "task_evidence": False,
        "budget_guidance": False,
        "pipeline_metrics": True,
        "multi_edit_tools": False,
        "post_edit_syntax_hook": False,
        "smart_tool_truncation": True,
        "navigation_prefilter": False,
        "multi_file_apply": False,
        "parallel_read_tools": True,
        "scoped_test_grants": False,
        "incremental_sandbox_snapshots": False,
        "import_aware_repomap": False,
        "subagent_exploration": False,
        "calibrated_context_budget": True,
        "sticky_context_checkpoint": True,
        "protect_fresh_tool_output": True,
        "include_linter": False,
        "soft_test_failure_reminder": False,
        "max_length_continuations": 3,
        "repeated_failure_soft_threshold": 3,
        "repeated_failure_hard_threshold": 6,
        "benchmark_allow_denied_tool_continue": True,
        "stream_repetition_detection": True,
        "stream_repetition_max_retries": 2,
    }
    defaults.update(overrides)
    return HarnessConfig.model_validate(defaults)


def long_horizon_deepseek_flash(
    *,
    context_window_tokens: int = 512_000,
    max_output_tokens: int = 65_536,
    max_wall_seconds: int = 3600,
    policy_preset: PolicyPreset = "benchmark",
    harness_profile: Literal["swe-python", "terminal-minimal"] = "swe-python",
) -> AppConfig:
    """Preset for long-horizon DeepSeek-Flash-1017 runs (probed: max_tokens=65536, effort=max)."""
    harness = (
        terminal_minimal_harness()
        if harness_profile == "terminal-minimal"
        else swe_python_harness(include_linter=False)
    )
    execution = (
        ExecutionConfig(default_timeout_seconds=120, max_timeout_seconds=1800)
        if harness_profile == "terminal-minimal"
        else ExecutionConfig()
    )
    return AppConfig(
        model=ModelConfig(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-flash",
            thinking="enabled",
            reasoning_effort="max",
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
            request_timeout_seconds=240,
            supports_streaming=True,
            supports_tools=True,
        ),
        limits=LimitsConfig(
            max_model_calls=None,
            max_tool_calls=300,
            max_wall_seconds=max_wall_seconds,
            max_input_tokens_total=80_000_000,
            max_output_tokens_total=2_000_000,
        ),
        execution=execution,
        policy=PolicyConfig(preset=policy_preset),
        retry=RetryConfig(max_retries=5, base_delay_seconds=1.0, max_delay_seconds=60.0),
        harness=harness,
    )


def load_config(workspace: Path, config_path: Path | None = None) -> AppConfig:
    """Read data-only TOML; no repository program or policy file is executed."""
    path = config_path if config_path is not None else workspace / "stellarweaver.toml"
    if not path.is_file():
        if config_path is not None:
            raise FileNotFoundError(f"Configuration file does not exist: {path}")
        return AppConfig()
    with path.open("rb") as file:
        return AppConfig.model_validate(tomllib.load(file))


def env_file_path(workspace: Path, env_path: Path | None = None) -> Path:
    """Prefer the launcher directory so target workspaces need not hold credentials."""
    if env_path is not None:
        return env_path.resolve()
    launcher_env = Path.cwd() / ".env"
    return launcher_env if launcher_env.is_file() else workspace / ".env"


def get_api_key(config: ModelConfig, workspace: Path, env_path: Path | None = None) -> str | None:
    """Resolve a referenced key without exporting .env into child-process environments."""
    value = os.environ.get(config.api_key_env)
    if value:
        return value
    path = env_file_path(workspace, env_path)
    if env_path is not None and not path.is_file():
        raise FileNotFoundError(f"Environment file does not exist: {path}")
    if path.is_file():
        value = dotenv_values(path, interpolate=False).get(config.api_key_env)
        if value:
            return value
    return None
