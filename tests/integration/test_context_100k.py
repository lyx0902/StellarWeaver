"""Cross-workspace defaults and retention of source at the larger input window."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from stellarweaver.config import AppConfig, ContextConfig, load_config
from stellarweaver.context import ContextManager
from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


async def test_deepseek_capability_tracks_default_and_explicit_workspace_config(
    tmp_path: Path,
) -> None:
    settings = load_config(tmp_path)
    provider = OpenAICompatibleChatProvider(settings.model, "offline-placeholder-not-a-real-key")
    try:
        assert provider.capabilities().context_window_tokens == 200_000
        assert settings.limits.max_input_tokens_total == 1_000_000
        assert FakeModel().capabilities().context_window_tokens == 200_000
    finally:
        await provider.aclose()
    (tmp_path / "stellarweaver.toml").write_text(
        "[model]\ncontext_window_tokens = 32768\n[limits]\nmax_input_tokens_total = 200000\n",
        encoding="utf-8",
    )
    settings = load_config(tmp_path)
    provider = OpenAICompatibleChatProvider(settings.model, "offline-placeholder-not-a-real-key")
    try:
        assert provider.capabilities().context_window_tokens == 32768
        assert settings.limits.max_input_tokens_total == 200_000
    finally:
        await provider.aclose()


async def test_large_read_remains_visible_at_100k_instead_of_repeated_small_previews(
    tmp_path: Path,
) -> None:
    marker = "END_OF_REQUIRED_SOURCE_EVIDENCE"
    source = "".join(f"# line {index}: " + "source detail " * 50 + "\n" for index in range(32))
    (tmp_path / "module.py").write_text(source + marker + "\n", encoding="utf-8")
    model: FakeModel
    captured: list[ModelRequest] = []

    def script(request: ModelRequest) -> AssistantReply:
        if not any(message.role == "tool" for message in request.messages):
            return AssistantReply(
                tool_calls=(ToolCall("source", "read_file", '{"path":"module.py"}'),),
                stop_reason="tool_calls",
            )
        captured.append(request)
        body = json.loads(
            next(message.content for message in request.messages if message.role == "tool")
        )
        assert marker in body["content"]
        assert not body.get("projection", {}).get("truncated")
        return AssistantReply(
            "The complete required source is available; no repeat read is needed."
        )

    model = FakeModel(script, context_window_tokens=100_000)
    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(model, registry, store=store).run(
            "Read module.py including its final marker once."
        )
        assert outcome.run_status == "completed"
        assert outcome.tool_calls == 1 and outcome.model_calls == 2
        # The former window forced a derived preview for this same source/history.
        old_projection = ContextManager().build(
            store.load_messages()[:-1],
            tuple(registry.schemas()),
            4096,
            32768,
            ContextConfig(),
        )
        assert old_projection.compacted
        old_body = json.loads(
            next(item.content for item in old_projection.messages if item.role == "tool")
        )
        assert marker not in old_body["content"]
        assert old_body["projection"]["truncated"]
        estimated = len(
            json.dumps(
                {
                    "messages": [asdict(item) for item in captured[-1].messages],
                    "tools": captured[-1].tools,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        )
        assert 26624 < estimated < 100_000 - 4096 - 2048


@pytest.mark.parametrize("window", [100_000, 200_000])
async def test_adaptive_workbench_projection_keeps_agent_running_past_context_overflow(
    tmp_path: Path,
    window: int,
) -> None:
    (tmp_path / "source.txt").write_text("scope evidence\n" * 80, encoding="utf-8")
    reasoning = "r" * 16_000
    model = FakeModel(
        [
            *(
                AssistantReply(
                    tool_calls=(ToolCall(str(index), "read_file", '{"path":"source.txt"}'),),
                    stop_reason="tool_calls",
                    reasoning_content=reasoning,
                    usage={"prompt_tokens": 70000, "completion_tokens": 2000},
                )
                for index in range(8)
            ),
            AssistantReply("Analysis finished"),
        ],
        context_window_tokens=window,
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path,
            PolicyEngine("read-only", workspace=tmp_path),
            artifacts_dir=store.artifacts_dir,
        )
        outcome = await Agent(
            model,
            registry,
            store=store,
            max_output_tokens=8192,
            enforce_run_budget=False,
            adaptive_round_retention=True,
        ).run("Read the entire repository and finish the analysis")
        assert outcome.run_status == "completed" and outcome.tool_calls == 8
        events = store.load_events()
        if window == 100_000:
            assert any(
                event.type == "context.compacted"
                and event.payload["strategy"] == "adaptive_complete_rounds"
                and event.payload["after_tokens_estimated"] <= 89760
                for event in events
            )
        else:
            assert any(
                event.type == "model.started"
                and 100_000 < event.payload["input_tokens"] <= 189_760
                and event.payload["context_window_tokens"] == 200_000
                for event in events
            )
        assert sum(item.role == "tool" for item in store.load_messages()) == 8
        assert any(
            item.role == "assistant" and item.reasoning_content == reasoning
            for item in model.requests[-1].messages
        )


def test_installed_cli_uses_new_defaults_from_another_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "another workspace"
    workspace.mkdir()
    entry = Path(sys.executable).parent / (
        "stellarweaver.exe" if os.name == "nt" else "stellarweaver"
    )
    result = subprocess.run(
        [
            str(entry),
            "exec",
            "--workspace",
            str(workspace),
            "--provider",
            "fake",
            "--policy",
            "read-only",
            "--prompt",
            "Read-only configuration smoke",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
            "--no-stream",
        ],
        cwd=workspace,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    outcome = events[-1]["payload"]
    snapshot = SessionStore.inspect(outcome["state_path"])
    settings = AppConfig.model_validate_json(snapshot.metadata["config_json"])
    assert settings.model.context_window_tokens == 200_000
    assert (
        next(event["payload"] for event in events if event["type"] == "model.started")[
            "context_window_tokens"
        ]
        == 200_000
    )
    assert settings.limits.max_input_tokens_total == 1_000_000
    assert outcome["run_status"] == "completed"
