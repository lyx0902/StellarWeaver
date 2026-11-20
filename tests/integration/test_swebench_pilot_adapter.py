"""Regression tests for the pilot's narrow command interface and prediction handoff."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from stellarweaver.evals import swebench_pilot as pilot


@pytest.mark.parametrize("change", [True, False])
async def test_pilot_checks_and_exports_only_valid_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: bool
) -> None:
    assets = tmp_path / "assets"
    source = assets / "marshmallow-1359"
    source.mkdir(parents=True)
    (assets / "test-deps").mkdir()
    pilot.write_json(
        assets / "agent-input.json",
        {"instance_id": pilot.INSTANCE, "problem_statement": "Fix the example.", "patch": "oracle"},
    )
    original = b"value = 0\n"
    (source / "module.py").write_bytes(original)
    pilot.git(source, "init", "-q")
    pilot.git(source, "add", ".")
    pilot.git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    base = pilot.git(source, "rev-parse", "HEAD").decode().strip()
    monkeypatch.setattr(pilot, "BASE", base)
    check_output = pilot.subprocess.check_output

    def inspect_image(argv, **kwargs):
        if argv[:3] == ["docker", "image", "inspect"]:
            return "sha256:local-fixture"
        return check_output(argv, **kwargs)

    calls = []

    async def run(runner, argv, cwd, **kwargs):
        assert cwd == runner.workspace
        calls.append(tuple(argv))
        success = (runner.workspace / "module.py").read_bytes() == b"value = 1\n"
        return {
            "status": "ok" if success else "error",
            "data": {
                "exit_code": 0 if success else 1,
                "stdout": "",
                "stderr": "" if success else pilot.EXPECTED_ERROR,
                "argv": argv,
                "cwd": ".",
            },
        }

    monkeypatch.setattr(pilot.subprocess, "check_output", inspect_image)
    monkeypatch.setattr(pilot.PilotRunner, "run", run)

    def tool(call_id, name, args):
        return {"call_id": call_id, "name": name, "arguments_json": json.dumps(args)}

    def reply(*tools):
        return {"tool_calls": list(tools), "stop_reason": "tool_calls"}

    script = tmp_path / "script.json"
    replies = (
        [
            reply(
                tool("old", "run_command", {"argv": ["git", "log", "--oneline", "-5"]}),
                tool("search", "search_text", {"pattern": "value"}),
            ),
            reply(tool("invalid", "run_visible_tests", {"argv": ["unexpected"]})),
            reply(tool("read", "read_file", {"path": "module.py"})),
            reply(
                tool(
                    "edit",
                    "edit_file",
                    {
                        "path": "module.py",
                        "old_text": "value = 0",
                        "new_text": "value = 1",
                        "expected_sha256": hashlib.sha256(original).hexdigest(),
                    },
                )
            ),
            reply(tool("visible", "run_visible_tests", {}), tool("repro", "run_reproducer", {})),
        ]
        if change
        else []
    )
    script.write_text(json.dumps([*replies, {"text": "Fixture complete."}]), encoding="utf-8")
    out = tmp_path / "out"
    code = await pilot.run_pilot(
        assets, out, tmp_path / ".env", False, None, replay=False, fake_script=script
    )
    report = json.loads((out / "agent-report.json").read_text(encoding="utf-8"))
    assert report["provider"] == "fake"
    assert report["official_evaluation"]["status"] == "not_evaluated"
    assert report["official_evaluation"]["resolved"] is None
    assert report["outcome"]["run_status"] == "completed"
    candidate = (out / "candidate.patch").read_bytes()
    assert report["candidate"]["sha256"] == hashlib.sha256(candidate).hexdigest()
    assert report["candidate"]["status"] == ("valid_patch" if change else "empty_patch")
    assert (out / "predictions.jsonl").exists() == change
    assert code == (0 if change else 1)
    assert calls == ([pilot.REPRO, pilot.TESTS, pilot.REPRO] if change else [pilot.REPRO])
    if change:
        tools = {row["tool_name"]: row for row in report["tools"]}
        assert tools["run_command"]["data"]["code"] == "UNKNOWN_TOOL"
        assert tools["search_text"]["status"] == "ok"
        assert any(row["data"].get("code") == "INVALID_ARGUMENTS" for row in report["tools"])
        assert tools["run_visible_tests"]["status"] == tools["run_reproducer"]["status"] == "ok"
        assert report["outcome"]["verification_status"] == "passed"
        assert (
            "+value = 1"
            in json.loads((out / "predictions.jsonl").read_text(encoding="utf-8"))["model_patch"]
        )
        # Keep the original source and its fixture-only metadata out of the task's changes.
        assert (source / "module.py").read_bytes() == original
        with sqlite3.connect(report["outcome"]["state_path"]) as conn:
            messages = [
                json.loads(row[0]) for row in conn.execute("SELECT body_json FROM messages")
            ]
            events = [json.loads(row[0]) for row in conn.execute("SELECT event_json FROM events")]
        prompt = next(row["content"] for row in messages if row["role"] == "user")
        assert base in prompt and pilot.REPO in prompt and "oracle" not in prompt
        grants = [row["payload"] for row in events if row["type"] == "tool.authorization"]
        assert {
            entry["tool"] for entry in grants if entry.get("authorization_tool") == "run_command"
        } == {
            "run_visible_tests",
            "run_reproducer",
        }
