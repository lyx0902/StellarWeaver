import json
from pathlib import Path

import pytest

from stellarweaver.config import LimitsConfig
from stellarweaver.domain import AssistantReply, ModelRequest, ToolCall
from stellarweaver.evals.swebench_pilot import EXPECTED_ERROR, REPRODUCER, PilotRunner, classify
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.loop import Agent, CompletionProof
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.workspace import sha256_bytes


def edit(old: str, new: str, call_id: str) -> AssistantReply:
    return AssistantReply(
        tool_calls=(
            ToolCall(
                call_id,
                "edit_file",
                json.dumps(
                    {
                        "path": "calc.py",
                        "old_text": old,
                        "new_text": new,
                        "expected_sha256": sha256_bytes(old.encode()),
                    }
                ),
            ),
        ),
        stop_reason="tool_calls",
    )


@pytest.mark.parametrize(
    ("repair", "model_limit", "tool_limit", "expected_status", "proofs", "feedback_count"),
    [
        (True, 10, 10, "completed", ["failed", "passed"], 1),
        (True, None, 10, "completed", ["failed", "passed"], 1),
        (False, 10, 10, "completed", ["failed", "failed"], 1),
        (True, 2, 10, "budget_exceeded", ["failed"], 0),
        (True, 10, 3, "budget_exceeded", ["failed"], 1),
    ],
)
async def test_completion_check_repair_and_budgets(
    tmp_path: Path,
    repair: bool,
    model_limit: int | None,
    tool_limit: int,
    expected_status: str,
    proofs: list[str],
    feedback_count: int,
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    source = root / "calc.py"
    old, bad, fixed = "result = 0\n", "result = 1\n", "result = 2\n"
    source.write_bytes(old.encode())
    checks: list[str] = []
    replies = iter(
        [edit(old, bad, "bad"), AssistantReply("ready")]
        + (
            [edit(bad, fixed, "fix"), AssistantReply("ready now")]
            if repair
            else [AssistantReply("still ready")]
        )
    )

    def answer(request: ModelRequest) -> AssistantReply:
        reply = next(replies)
        if checks:
            assert any("expected two" in message.content for message in request.messages)
        return reply

    async def check(seconds: float) -> CompletionProof:
        assert seconds > 0
        observed = source.read_text(encoding="utf-8")
        checks.append(observed)
        if observed == fixed:
            return CompletionProof(
                "passed", "reproducer_passed", {"snapshot_sha256": sha256_bytes(observed.encode())}
            )
        return CompletionProof("failed", "original_error_remains", {"diagnostic": "expected two"})

    with SessionStore(root, data_dir=tmp_path / "state") as store:
        agent = Agent(
            FakeModel(answer),
            ToolRegistry(root, PolicyEngine("workspace-edit", workspace=root)),
            store=store,
            limits=LimitsConfig(max_model_calls=model_limit, max_tool_calls=tool_limit),
            completion_check=check,
        )
        outcome = await agent.run("Fix the observed defect.")
        events = store.load_events()
    assert outcome.run_status == expected_status
    assert outcome.verification_status == "not_run"
    assert [
        item["status"] for item in outcome.evidence if item["kind"] == "completion_check"
    ] == proofs
    assert [event.type for event in events].count("reproduction.feedback") == feedback_count
    assert len(checks) == len(proofs)


@pytest.mark.parametrize(
    ("code", "stderr", "timed_out", "baseline", "expected"),
    [
        (1, EXPECTED_ERROR, False, True, ("passed", "baseline_reproduced")),
        (1, EXPECTED_ERROR, False, False, ("failed", "original_error_remains")),
        (0, "", False, False, ("passed", "reproducer_passed")),
        (0, "", False, True, ("unavailable", "baseline_did_not_reproduce")),
        (125, "Docker error", False, False, ("unavailable", "runner_unavailable")),
        (
            1,
            "ModuleNotFoundError: No module named 'pytest'",
            False,
            True,
            ("unavailable", "dependency_or_import_error"),
        ),
        (1, "", True, False, ("unavailable", "verification_timeout")),
    ],
)
def test_reproducer_classification(code, stderr, timed_out, baseline, expected):
    result = {
        "status": "error" if code else "ok",
        "data": {"exit_code": code, "stderr": stderr, "timed_out": timed_out},
    }
    assert classify(result, baseline=baseline) == expected


async def test_changed_candidate_invalidates_success(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    project.mkdir()
    source = project / "module.py"
    source.write_text("before", encoding="utf-8")
    repro = tmp_path / "repro"
    repro.mkdir()
    (repro / "reproduce.py").write_bytes(REPRODUCER)
    runner = PilotRunner(project, tmp_path / "deps", repro, "local-image")

    async def changed(*args, **kwargs):
        source.write_text("after", encoding="utf-8")
        return {"status": "ok", "data": {"exit_code": 0}}

    monkeypatch.setattr(runner, "run", changed)
    proof = await runner.check(5)
    assert (proof.status, proof.reason) == ("unavailable", "verification_inputs_changed")
