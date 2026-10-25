"""Explicit extension inputs must reach the real CLI composition safely."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stellarweaver.cli import app
from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.providers.fake import FakeModel

runner = CliRunner()


def write_skill(root: Path, name: str) -> None:
    target = root / name / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        f"---\nname: {name}\ndescription: Demonstrate {name}\n---\nPRIVATE_BODY_{name}\n"
    )


@pytest.mark.parametrize("trusted", [False, True])
def test_project_skills_require_the_explicit_trust_cli_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted: bool,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    write_skill(workspace / ".stellarweaver" / "skills", "project-skill")

    def reply(request: ModelRequest) -> AssistantReply:
        assert any(tool["function"]["name"] == "load_skill" for tool in request.tools) is trusted
        assert any("project-skill" in message.content for message in request.messages) is trusted
        assert all(
            "PRIVATE_BODY_project-skill" not in message.content for message in request.messages
        )
        return AssistantReply("Only the allowed metadata was observed.")

    model = FakeModel(callback=reply)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    arguments = [
        "exec",
        "inspect extensions",
        "-w",
        str(workspace),
        "--provider",
        "fake",
        "--data-dir",
        str(tmp_path / "state"),
        "--json",
    ]
    if trusted:
        arguments.append("--trust-workspace")
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert len(model.requests) == 1
    events = [json.loads(line) for line in result.output.splitlines()]
    assert any(event["type"] == "skills.discovered" for event in events) is trusted


def test_repeated_explicit_user_skill_dirs_are_discovered_and_one_body_loads_through_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    first, second = tmp_path / "first-skills", tmp_path / "second-skills"
    write_skill(first, "alpha")
    write_skill(second, "beta")
    count = 0

    def reply(request: ModelRequest) -> AssistantReply:
        nonlocal count
        count += 1
        if count == 1:
            assert any(
                "alpha" in message.content and "beta" in message.content
                for message in request.messages
                if message.role == "system"
            )
            assert all("PRIVATE_BODY_" not in message.content for message in request.messages)
            return AssistantReply(
                tool_calls=(ToolCall("load-alpha", "load_skill", json.dumps({"name": "alpha"})),),
                stop_reason="tool_calls",
            )
        assert any(
            message.role == "tool" and "PRIVATE_BODY_alpha" in message.content
            for message in request.messages
        )
        assert all("PRIVATE_BODY_beta" not in message.content for message in request.messages)
        return AssistantReply("The requested alpha body was loaded; beta stayed metadata only.")

    model = FakeModel(callback=reply)
    monkeypatch.setattr("stellarweaver.cli.build_provider", lambda *args, **kwargs: model)
    result = runner.invoke(
        app,
        [
            "exec",
            "load alpha",
            "-w",
            str(workspace),
            "--provider",
            "fake",
            "--skill-dir",
            str(first),
            "--skill-dir",
            str(second),
            "--policy",
            "read-only",
            "--data-dir",
            str(tmp_path / "state"),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(model.requests) == 2
    events = [json.loads(line) for line in result.output.splitlines()]
    assert any(event["type"] == "skill.loaded" for event in events)
    assert any(
        event["type"] == "tool.completed"
        and event["payload"]["tool_name"] == "load_skill"
        and event["payload"]["status"] == "ok"
        for event in events
    )


@pytest.mark.parametrize("command", ["exec", "chat", "resume"])
def test_extension_cli_options_are_available_on_all_execution_entries(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    output = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    for flag in ("--extensions", "--trust-workspace", "--skill-dir"):
        assert flag in output
