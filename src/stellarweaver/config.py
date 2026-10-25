"""Strict configuration, with API credentials kept outside serializable settings."""

import os
import tomllib
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ModelConfig(StrictConfig):
    provider: Literal["fake", "deepseek", "openai-compatible-chat"] = "fake"
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    api_key_env: str = "STELLARWEAVER_API_KEY"
    context_window_tokens: int = Field(default=100_000, ge=2048)
    max_output_tokens: int = Field(default=4096, ge=1)
    request_timeout_seconds: int = Field(default=90, ge=1, le=600)
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
    max_model_calls: int = Field(default=30, ge=1)
    max_tool_calls: int = Field(default=100, ge=1)
    max_wall_seconds: int = Field(default=900, ge=1)
    max_input_tokens_total: int = Field(default=1_000_000, ge=1)
    max_output_tokens_total: int = Field(default=30000, ge=1)


class ExecutionConfig(StrictConfig):
    backend: Literal["host"] = "host"
    default_timeout_seconds: int = Field(default=60, ge=1)
    max_timeout_seconds: int = Field(default=300, ge=1)
    max_output_bytes_per_stream: int = Field(default=1048576, ge=1024)

    @model_validator(mode="after")
    def timeout_order(self) -> "ExecutionConfig":
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("default_timeout_seconds must not exceed max_timeout_seconds")
        return self


class PolicyConfig(StrictConfig):
    preset: Literal["ask", "read-only", "workspace-edit"] = "ask"


class ContextConfig(StrictConfig):
    trigger_ratio: float = Field(default=0.80, gt=0, lt=1)
    target_ratio: float = Field(default=0.55, gt=0, lt=1)
    reserve_tokens: int = Field(default=2048, ge=0)
    recent_complete_rounds: int = Field(default=6, ge=1)

    @model_validator(mode="after")
    def threshold_order(self) -> "ContextConfig":
        if self.target_ratio >= self.trigger_ratio:
            raise ValueError("target_ratio must be smaller than trigger_ratio")
        return self


class RetryConfig(StrictConfig):
    max_retries: int = Field(default=2, ge=0, le=2)
    base_delay_seconds: float = Field(default=0.5, ge=0, le=30)
    max_delay_seconds: float = Field(default=30.0, gt=0, le=60)


class AppConfig(StrictConfig):
    model: ModelConfig = Field(default_factory=ModelConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)


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
