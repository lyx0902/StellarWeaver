"""Unit tests for Web execution environment variable parsing and safety boundaries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stellarweaver.web.execution import web_harness_config, web_retry_config

_ENV_KEYS = (
    "SW_WEB_INCLUDE_LINTER",
    "SW_WEB_SOFT_TEST_FAILURE_REMINDER",
    "SW_WEB_MAX_LENGTH_CONTINUATIONS",
    "SW_WEB_STREAM_REPETITION_DETECTION",
    "SW_WEB_REPEATED_FAILURE_SOFT_THRESHOLD",
    "SW_WEB_REPEATED_FAILURE_HARD_THRESHOLD",
    "SW_WEB_MAX_RETRIES",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_web_execution_defaults_stay_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    harness = web_harness_config(repair=True, development_tools=True)
    retry = web_retry_config()

    assert harness.profile == "custom"
    assert harness.include_linter is True
    assert harness.soft_test_failure_reminder is False
    assert harness.max_length_continuations == 0
    assert harness.stream_repetition_detection is False
    assert harness.repeated_failure_soft_threshold == 3
    assert harness.repeated_failure_hard_threshold == 3
    assert harness.benchmark_allow_denied_tool_continue is False
    assert retry.max_retries == 2


def test_web_execution_parses_phase12_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("SW_WEB_INCLUDE_LINTER", "0")
    monkeypatch.setenv("SW_WEB_SOFT_TEST_FAILURE_REMINDER", "1")
    monkeypatch.setenv("SW_WEB_MAX_LENGTH_CONTINUATIONS", "3")
    monkeypatch.setenv("SW_WEB_STREAM_REPETITION_DETECTION", "1")
    monkeypatch.setenv("SW_WEB_REPEATED_FAILURE_SOFT_THRESHOLD", "3")
    monkeypatch.setenv("SW_WEB_REPEATED_FAILURE_HARD_THRESHOLD", "6")
    monkeypatch.setenv("SW_WEB_MAX_RETRIES", "4")

    harness = web_harness_config(repair=True, development_tools=True)
    retry = web_retry_config()

    assert harness.include_linter is False
    assert harness.soft_test_failure_reminder is True
    assert harness.max_length_continuations == 3
    assert harness.stream_repetition_detection is True
    assert harness.repeated_failure_soft_threshold == 3
    assert harness.repeated_failure_hard_threshold == 6
    assert harness.benchmark_allow_denied_tool_continue is False
    assert retry.max_retries == 4


def test_web_execution_reuses_config_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("SW_WEB_REPEATED_FAILURE_SOFT_THRESHOLD", "5")
    monkeypatch.setenv("SW_WEB_REPEATED_FAILURE_HARD_THRESHOLD", "3")
    with pytest.raises(ValidationError):
        web_harness_config(repair=True, development_tools=False)

    _clear_env(monkeypatch)
    monkeypatch.setenv("SW_WEB_MAX_RETRIES", "9")
    with pytest.raises(ValidationError):
        web_retry_config()

    _clear_env(monkeypatch)
    monkeypatch.setenv("SW_WEB_MAX_RETRIES", "not-an-int")
    with pytest.raises(ValueError, match="SW_WEB_MAX_RETRIES must be an integer"):
        web_retry_config()
