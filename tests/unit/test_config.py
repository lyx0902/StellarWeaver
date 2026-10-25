import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from stellarweaver.config import AppConfig, ExecutionConfig, ModelConfig, get_api_key, load_config


def test_defaults_are_offline_and_strict(tmp_path: Path) -> None:
    assert load_config(tmp_path).model.provider == "fake"
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"limits": {"max_model_calls": "3"}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"unexpected": True})
    with pytest.raises(ValidationError):
        ExecutionConfig(default_timeout_seconds=20, max_timeout_seconds=10)


def test_load_toml_and_missing_explicit_config(tmp_path: Path) -> None:
    (tmp_path / "stellarweaver.toml").write_text('[model]\nprovider = "deepseek"\n')
    assert load_config(tmp_path).model.model == "deepseek-flash"
    assert load_config(tmp_path).model.provider == "deepseek"
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path, tmp_path / "absent.toml")


def test_env_credentials_do_not_leak_to_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STELLARWEAVER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("STELLARWEAVER_API_KEY=offline-test-secret\n")
    target = tmp_path / "target"
    target.mkdir()
    assert get_api_key(ModelConfig(), target) == "offline-test-secret"
    assert "STELLARWEAVER_API_KEY" not in os.environ
    monkeypatch.setenv("STELLARWEAVER_API_KEY", "process-value")
    assert get_api_key(ModelConfig(), target) == "process-value"


def test_explicit_env_file_is_not_exported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STELLARWEAVER_API_KEY", raising=False)
    path = tmp_path / "separate.env"
    path.write_text("STELLARWEAVER_API_KEY=literal-${NOT_EXPANDED}\n")
    assert get_api_key(ModelConfig(), tmp_path, path) == "literal-${NOT_EXPANDED}"
    with pytest.raises(FileNotFoundError):
        get_api_key(ModelConfig(), tmp_path, tmp_path / "missing")


def test_model_window_and_context_validation() -> None:
    with pytest.raises(ValidationError):
        ModelConfig(context_window_tokens=4096, max_output_tokens=4096)
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"context": {"trigger_ratio": 0.2, "target_ratio": 0.5}})
