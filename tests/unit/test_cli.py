import io
import json
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stellarweaver.cli import app
from stellarweaver.config import ModelConfig
from stellarweaver.domain import (
    AgentEvent,
    AssistantReply,
    ModelCapabilities,
    ModelRequest,
    ModelStreamEvent,
    RunOutcome,
    TextDelta,
)
from stellarweaver.providers.base import ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.ui.console import TerminalUI

runner = CliRunner()


def test_json_events_round_trip_unicode_on_real_gbk_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
    payload = {"text": "修复完成 🚀", "path": "测试🧪.py"}
    event = AgentEvent("session", "run", 1, "assistant.completed", payload, "2026-10-25")
    with monkeypatch.context() as patcher:
        patcher.setattr(sys, "stdout", stream)
        TerminalUI(json_output=True).event(event)
        stream.flush()
        output = buffer.getvalue()
    assert output.isascii()
    assert json.loads(output.decode("gbk"))["payload"] == payload


def test_help_and_version() -> None:
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert "stellarweaver 0.1.0" in runner.invoke(app, ["--version"]).output
    assert runner.invoke(app, ["version"]).exit_code == 0
    assert "schema" in runner.invoke(app, ["version"]).output
    exec_help = re.sub(r"\x1b\[[0-9;]*m", "", runner.invoke(app, ["exec", "--help"]).output)
    assert "--policy-file" in exec_help


def test_doctor_offline_redacts_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STELLARWEAVER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("STELLARWEAVER_API_KEY=do-not-print-this\n")
    result = runner.invoke(app, ["doctor", "--offline", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "API key: present" in result.output
    assert "do-not-print-this" not in result.output
    assert "Model connectivity: not tested" in result.output


def test_minimal_exec_json_and_session_records(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "exec",
            "--prompt",
            "Inspect offline",
            "--workspace",
            str(tmp_path),
            "--provider",
            "fake",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in result.output.splitlines()]
    assert all("schema_version" in event for event in lines)
    durable = [event for event in lines if event["type"] != "text.delta"]
    assert [event["seq"] for event in durable] == list(range(1, len(durable) + 1))
    assert all("seq" not in event for event in lines if event["type"] == "text.delta")
    assert lines[-1]["type"] == "run.completed"
    outcome = lines[-1]["payload"]
    assert outcome["verification_status"] == "not_run"
    assert "Offline FakeModel" in outcome["final_text"]
    assert Path(outcome["state_path"]).is_file()


def test_exec_no_tty_does_not_prompt_or_write(tmp_path: Path) -> None:
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "one",
                            "name": "create_file",
                            "arguments_json": json.dumps({"path": "new.txt", "content": "new"}),
                        }
                    ],
                    "stop_reason": "tool_calls",
                }
            ]
        )
    )
    result = runner.invoke(
        app,
        [
            "exec",
            "write",
            "--workspace",
            str(tmp_path),
            "--provider",
            "fake",
            "--script",
            str(script),
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 3, result.output
    assert not (tmp_path / "new.txt").exists()
    assert json.loads(result.output.splitlines()[-1])["payload"]["run_status"] == "blocked"


def test_chat_without_tty_fails_immediately(tmp_path: Path) -> None:
    result = runner.invoke(app, ["chat", "--workspace", str(tmp_path)])
    assert result.exit_code == 2
    assert "requires an interactive TTY" in result.output


def test_missing_key_gives_exact_env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STELLARWEAVER_API_KEY", raising=False)
    env_file = tmp_path / "provider.env"
    env_file.write_text("STELLARWEAVER_API_KEY=\n")
    result = runner.invoke(
        app,
        [
            "exec",
            "hello",
            "--workspace",
            str(tmp_path),
            "--provider",
            "deepseek",
            "--env-file",
            str(env_file),
        ],
    )
    assert result.exit_code == 2
    assert str(env_file) in result.output.replace("\n", "")
    assert "API key missing" in result.output


def test_invalid_config_does_not_echo_credential(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[model]\napi_key = "must-not-appear"\n')
    result = runner.invoke(
        app, ["exec", "hello", "--workspace", str(tmp_path), "--config", str(config)]
    )
    assert result.exit_code == 2
    assert "must-not-appear" not in result.output
    assert "Extra inputs are not permitted" in " ".join(result.output.split())


def test_exec_requires_one_prompt_source() -> None:
    assert runner.invoke(app, ["exec"]).exit_code == 2
    assert runner.invoke(app, ["exec", "positional", "--prompt", "option"]).exit_code == 2


def test_failed_verification_exit_five(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", "raise SystemExit(1)"]
    policy_file = tmp_path / "grants.json"
    policy_file.write_text(
        json.dumps(
            {
                "workspace": str(tmp_path),
                "grants": [{"tool": "run_command", "argv": argv, "cwd": "."}],
            }
        )
    )
    script = tmp_path / "script.json"
    script.write_text(
        json.dumps(
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "check",
                            "name": "run_command",
                            "arguments_json": json.dumps({"argv": argv}),
                        }
                    ],
                    "stop_reason": "tool_calls",
                },
                {"text": "Check failed; task remains unresolved."},
            ]
        )
    )
    result = runner.invoke(
        app,
        [
            "exec",
            "--prompt",
            "verify",
            "--workspace",
            str(tmp_path),
            "--provider",
            "fake",
            "--script",
            str(script),
            "--policy-file",
            str(policy_file),
            "--data-dir",
            str(tmp_path / "state"),
            "--fail-on-verification-error",
            "--json",
        ],
    )
    assert result.exit_code == 5, result.output
    final = json.loads(result.output.splitlines()[-1])["payload"]
    assert final["run_status"] == "completed"
    assert final["verification_status"] == "failed"


def test_doctor_check_model_requires_explicit_real_provider(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--check-model", "-w", str(tmp_path)])
    assert result.exit_code == 2
    assert "--provider deepseek" in result.output
    assert "connected" not in result.output


def test_doctor_offline_and_check_model_are_exclusive(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--offline", "--check-model", "-w", str(tmp_path)])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_doctor_explicit_model_check_is_single_bounded_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "STELLARWEAVER_DEEPSEEK_OK"

    def respond_to_instruction(request: ModelRequest) -> AssistantReply:
        instruction = request.messages[-1].content
        assert instruction.startswith("Reply exactly: ")
        return AssistantReply(
            instruction.removeprefix("Reply exactly: "), usage={"total_tokens": 15}
        )

    model = FakeModel(callback=respond_to_instruction)
    monkeypatch.setenv("STELLARWEAVER_API_KEY", "secret-not-for-output")
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        ["doctor", "--check-model", "--provider", "deepseek", "-w", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "Model connectivity: connected" in result.output
    assert marker in result.output
    assert "secret-not-for-output" not in result.output
    assert len(model.requests) == 1
    assert model.requests[0].max_output_tokens == 64
    assert not model.requests[0].tools
    assert model.requests[0].messages[-1].role == "user"
    assert model.requests[0].messages[-1].content == "Reply exactly: " + marker
    assert model.requests[0].messages[0].content == (
        "This is a connectivity check. Return only the exact text requested."
    )


def test_doctor_missing_key_prints_env_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STELLARWEAVER_API_KEY", raising=False)
    env_file = tmp_path / "missing-key.env"
    env_file.write_text("STELLARWEAVER_API_KEY=\n")
    result = runner.invoke(
        app,
        [
            "doctor",
            "--check-model",
            "--provider",
            "deepseek",
            "-w",
            str(tmp_path),
            "--env-file",
            str(env_file),
        ],
    )
    assert result.exit_code == 2
    assert "API key missing" in result.output
    assert str(env_file) in result.output


@pytest.mark.parametrize(
    "reply", [AssistantReply("unexpected"), AssistantReply("", stop_reason="length")]
)
def test_doctor_never_claims_connectivity_for_incomplete_or_wrong_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply: AssistantReply
) -> None:
    monkeypatch.setenv("STELLARWEAVER_API_KEY", "not-for-output")
    monkeypatch.setattr(
        "stellarweaver.cli.build_provider", lambda *args, **kwargs: FakeModel([reply])
    )
    result = runner.invoke(
        app, ["doctor", "--check-model", "--provider", "deepseek", "-w", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "Model API responded" in result.output
    assert "expected complete marker" in " ".join(result.output.split())
    assert "visible_text_length=" in result.output
    assert "Model connectivity: connected" not in result.output


def test_doctor_marker_mismatch_does_not_echo_arbitrary_vendor_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STELLARWEAVER_API_KEY", "not-for-output")
    reply = AssistantReply("vendor-private-content", stop_reason="vendor-private-stop")
    monkeypatch.setattr(
        "stellarweaver.cli.build_provider", lambda *args, **kwargs: FakeModel([reply])
    )
    result = runner.invoke(
        app, ["doctor", "--check-model", "--provider", "deepseek", "-w", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "Model API responded" in result.output
    assert "stop_reason=other" in result.output
    assert f"visible_text_length={len(reply.text)}" in result.output
    assert reply.text not in result.output
    assert reply.stop_reason not in result.output


def test_doctor_provider_error_uses_sanitized_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(request: object) -> AssistantReply:
        raise ProviderError("Model API returned HTTP 401; check provider configuration")

    monkeypatch.setenv("STELLARWEAVER_API_KEY", "not-for-output")
    monkeypatch.setattr(
        "stellarweaver.cli.build_provider", lambda *args, **kwargs: FakeModel(callback=fail)
    )
    result = runner.invoke(
        app, ["doctor", "--check-model", "--provider", "deepseek", "-w", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "HTTP 401" in result.output
    assert "not-for-output" not in result.output


@pytest.mark.parametrize("flag", ["--stream", "--no-stream"])
def test_exec_accepts_explicit_stream_control(tmp_path: Path, flag: str) -> None:
    result = runner.invoke(
        app,
        [
            "exec",
            "offline",
            "-w",
            str(tmp_path),
            "--provider",
            "fake",
            flag,
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.output.splitlines()]
    assert any(event["type"] == "text.delta" for event in events) == (flag == "--stream")


@pytest.mark.parametrize(
    ("configured", "flag", "expected_stream", "fallback"),
    [
        (False, None, False, True),
        (False, "--stream", True, False),
        (True, "--no-stream", False, False),
        (True, None, True, False),
    ],
)
def test_cli_respects_streaming_configuration_unless_explicitly_overridden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
    flag: str | None,
    expected_stream: bool,
    fallback: bool,
) -> None:
    models: list[FakeModel] = []
    seen_configs: list[ModelConfig] = []

    class ConfiguredProvider(FakeModel):
        def __init__(self, config: ModelConfig, api_key: str) -> None:
            super().__init__([AssistantReply("offline transport fixture")])
            self.config = config
            models.append(self)
            seen_configs.append(config)

        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities(streaming=self.config.supports_streaming)

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
            assert self.config.supports_streaming, "Streaming must respect configured capability"
            async for event in super().stream(request):
                yield event

        async def aclose(self) -> None:
            pass

    monkeypatch.setenv("STELLARWEAVER_API_KEY", "offline-test-key")
    monkeypatch.setattr("stellarweaver.cli.OpenAICompatibleChatProvider", ConfiguredProvider)
    (tmp_path / "stellarweaver.toml").write_text(
        f'[model]\nprovider = "deepseek"\nsupports_streaming = {str(configured).lower()}\n'
    )
    args = [
        "exec",
        "offline fixture",
        "-w",
        str(tmp_path),
        "--data-dir",
        str(tmp_path / "state"),
        "--json",
    ]
    if flag is not None:
        args.append(flag)
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert len(models) == 1
    assert seen_configs[0].supports_streaming is expected_stream
    events = [json.loads(line) for line in result.output.splitlines()]
    started = next(event for event in events if event["type"] == "model.started")
    assert started["payload"]["streaming"] is expected_stream
    assert started["payload"]["stream_fallback"] is fallback
    assert any(event["type"] == "text.delta" for event in events) is expected_stream


def test_json_delta_is_ascii_safe_and_has_independent_chunk_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="gbk", errors="strict")
    delta = TextDelta("session", "run", 1, 2, "完成 🚀")
    with monkeypatch.context() as patcher:
        patcher.setattr(sys, "stdout", stream)
        TerminalUI(json_output=True).delta(delta)
        stream.flush()
        output = buffer.getvalue()
    value = json.loads(output.decode("gbk"))
    assert output.isascii()
    assert value["type"] == "text.delta"
    assert value["chunk_index"] == 2
    assert value["text"] == "完成 🚀"
    assert "seq" not in value


def test_streamed_final_reply_is_displayed_once_and_interruption_is_explicit() -> None:
    from rich.console import Console

    output = io.StringIO()
    ui = TerminalUI()
    ui.console = Console(file=output, force_terminal=False, width=200)
    ui.delta(TextDelta("session", "run", 1, 1, "discarded draft"))
    ui.event(AgentEvent("session", "run", 1, "model.interrupted", {}, "now"))
    ui.delta(TextDelta("session", "run", 2, 1, "final reply"))
    ui.event(
        AgentEvent("session", "run", 2, "assistant.completed", {"content": "final reply"}, "now")
    )
    ui.outcome(RunOutcome("completed", "unknown", "not_run", final_text="final reply"))
    text = output.getvalue()
    assert text.count("final reply") == 1
    assert "displayed draft discarded" in text


def test_stream_fallback_is_explicit_in_terminal_output() -> None:
    from rich.console import Console

    output = io.StringIO()
    ui = TerminalUI()
    ui.console = Console(file=output, force_terminal=False, width=200)
    ui.event(AgentEvent("session", "run", 1, "model.started", {"stream_fallback": True}, "now"))
    assert "streaming unavailable" in output.getvalue()
