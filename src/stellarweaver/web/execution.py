"""Agent execution shared by the inline server and the queue worker."""

import asyncio
import json
import logging
import os
from dataclasses import asdict
from threading import Event
from typing import cast
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from stellarweaver.config import AppConfig, HarnessConfig, ModelConfig, PolicyConfig
from stellarweaver.domain import (
    AssistantReply,
    JSONDict,
    RunOutcome,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from stellarweaver.policy.risk import fingerprint
from stellarweaver.providers.base import ModelProvider
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.service import build_agent
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web import db
from stellarweaver.web.approval import VERIFY_ARGV, ApprovalGate
from stellarweaver.web.isolation import DockerRunner
from stellarweaver.web.journal import JobOwnershipLost, PgJournal
from stellarweaver.web.offline import offline_repair_model
from stellarweaver.web.remote_runner import RemoteRunner
from stellarweaver.web.review import apply_selection

Runner = DockerRunner | RemoteRunner


def web_model_config(provider: str) -> ModelConfig:
    if provider == "fake":
        return ModelConfig(supports_streaming=True)
    overrides = {
        field: os.environ[name]
        for field, name in (
            ("thinking", "SW_WEB_DEEPSEEK_THINKING"),
            ("reasoning_effort", "SW_WEB_DEEPSEEK_REASONING_EFFORT"),
        )
        if name in os.environ
    }
    return ModelConfig.model_validate(
        {"provider": "deepseek", "supports_streaming": True, **overrides}
    )


def offline_repository_model() -> FakeModel:
    return FakeModel(
        [
            AssistantReply(
                tool_calls=(ToolCall("offline-list", "list_files", '{"path":"."}'),),
                stop_reason="tool_calls",
            ),
            AssistantReply(
                "Offline demo finished. Inspect the actual file listing in the timeline."
            ),
        ]
    )


async def run(
    journal: PgJournal,
    prompt: str,
    stop: Event,
    provider: str,
    api_key: str | None,
    approval_ttl: float,
    runner: Runner | None,
    continuation: str | None,
    review: JSONDict | None,
) -> None:
    repair = journal.delivery is not None
    if journal.delivery:
        journal.delivery.begin()
    config = AppConfig(
        model=web_model_config(provider),
        policy=PolicyConfig(preset="ask" if repair else "read-only"),
        harness=HarnessConfig(
            development_tools=bool(repair and runner and runner.isolation.development),
            issue_navigation=os.getenv("SW_WEB_ISSUE_NAVIGATION") == "1",
            patch_staging=bool(repair and os.getenv("SW_WEB_PATCH_STAGING") == "1"),
            task_evidence=os.getenv("SW_WEB_TASK_EVIDENCE") == "1",
            budget_guidance=os.getenv("SW_WEB_BUDGET_GUIDANCE") == "1",
            repair_controller=(
                "enforce"
                if os.getenv("SW_WEB_REPAIR_CONTROLLER") == "enforce"
                else "feedback"
                if os.getenv("SW_WEB_REPAIR_CONTROLLER") == "feedback"
                else "off"
            ),
        ),
    )
    commands = runner.isolation.commands if runner else (tuple(VERIFY_ARGV),)
    if runner and journal.delivery is not None:
        journal.delivery.required_commands = commands
    model: ModelProvider | None = None
    task: asyncio.Task[None] | asyncio.Task[RunOutcome]
    try:
        if stop.is_set() or (journal.claim is not None and journal.cancellation_requested()):
            raise asyncio.CancelledError()
        if review is not None:
            assert runner is not None
            task = asyncio.create_task(verify_selection(journal, runner, review))
        else:
            if provider == "fake":
                if journal.chat:
                    model = FakeModel([AssistantReply("这是离线普通对话的演示回复。")])
                else:
                    model = offline_repair_model(commands) if repair else offline_repository_model()
            else:
                assert api_key is not None
                model = OpenAICompatibleChatProvider(config.model, api_key)
            gate = ApprovalGate(
                journal,
                stop,
                approval_ttl,
                commands,
                development=config.harness.development_tools,
            )

            def on_delta(delta: TextDelta) -> None:
                for offset in range(0, len(delta.text), 4096):
                    journal.append_event(
                        "assistant.delta",
                        {
                            "content": delta.text[offset : offset + 4096],
                            "request_index": delta.request_index,
                        },
                    )

            agent = build_agent(
                config,
                model,
                journal,
                interactive=True,
                approval=gate.approve,
                on_delta=on_delta,
                command_runner=runner,
                command_boundary=fingerprint(
                    {
                        "isolation": asdict(runner.isolation),
                        "transport": runner.endpoint
                        if isinstance(runner, RemoteRunner)
                        else "docker",
                    }
                )
                if runner is not None
                else None,
                chat_only=journal.chat,
                enable_web_search=True,
            )
            interruption = journal.latest_interruption() if continuation else None
            if interruption is not None:
                journal.append_event("run.recovery_context", interruption)
                prompt += (
                    "\n\nPrevious Web run was interrupted. Do not assume that its unfinished "
                    "operations succeeded and do not replay them automatically. Inspect the "
                    "current workspace before making changes. Operator-visible recovery record: "
                    + json.dumps(interruption)
                )
            if repair:
                prompt += (
                    "\n\nWorkbench repair mode: each edit or verification requires browser permission. "
                    f"Configured verification argv choices: {json.dumps(commands)} "
                    'with cwd=".". Respect a denied operation and stop.'
                )
            history = journal.prior_turns() if continuation == "history" else ()
            task = asyncio.create_task(agent.run(prompt, history=history))

        async def monitor() -> None:
            try:
                while not task.done():
                    if stop.is_set() or (
                        journal.claim is not None and journal.cancellation_requested()
                    ):
                        task.cancel()
                        return
                    await asyncio.sleep(0.2 if journal.claim is not None else 0.05)
            except (SQLAlchemyError, JobOwnershipLost):
                journal.connection_interrupted = True
                if not task.done():
                    task.cancel()
                raise

        watcher = asyncio.create_task(monitor())
        try:
            await task
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    finally:
        if isinstance(model, OpenAICompatibleChatProvider):
            await model.aclose()


async def verify_selection(journal: PgJournal, runner: Runner, review: JSONDict) -> None:
    assert journal.delivery is not None
    journal.append_event("review.applying", {"source_run_id": review["run_id"]})
    apply_selection(journal.workspace, review["base"], review["patch"].encode())
    journal.append_event("review.applied", {"source_run_id": review["run_id"]})
    for argv in runner.isolation.commands:
        identifier = str(uuid4())
        journal.append_event("verification.started", {"argv": list(argv)})
        journal.delivery.started({"tool": "run_command", "operation_id": identifier})
        result = await runner(list(argv), journal.workspace, artifacts_dir=journal.artifacts_dir)
        journal.delivery.result(
            ToolResult(
                identifier,
                identifier,
                "run_command",
                cast(ToolStatus, result["status"]),
                result["content"],
                result["data"],
            )
        )
        journal.append_event(
            "verification.completed",
            {
                "argv": list(argv),
                "status": result["status"],
                "exit_code": result["data"]["exit_code"],
                "code": result["data"].get("code"),
            },
        )
        if result["status"] == "cancelled":
            raise asyncio.CancelledError()
        if result["status"] == "unknown":
            raise ToolError(
                "RUNNER_CLEANUP_UNCONFIRMED", "Verification requires executor reconciliation."
            )
    journal.finish_run({"run_status": "completed"}, "run.completed")


def execute(
    journal: PgJournal,
    prompt: str,
    stop: Event,
    provider: str,
    api_key: str | None,
    approval_ttl: float,
    runner: Runner | None = None,
    continuation: str | None = None,
    review: JSONDict | None = None,
) -> None:
    try:
        asyncio.run(
            run(
                journal, prompt, stop, provider, api_key, approval_ttl, runner, continuation, review
            )
        )
    except JobOwnershipLost:
        return
    except (Exception, asyncio.CancelledError) as exc:
        if not isinstance(exc, asyncio.CancelledError):
            logging.getLogger(__name__).error("Run failed: %s", type(exc).__name__)
        row = db.snapshot(journal.engine, journal.tenant, journal.run_id)
        if row is not None and row["status"] == "active":
            status = (
                "cancelled"
                if stop.is_set() or isinstance(exc, asyncio.CancelledError)
                else "failed"
            )
            outcome = {"run_status": status, "verification_status": "not_run"}
            if status == "failed" and isinstance(exc, SQLAlchemyError):
                outcome.update(error_code="JOURNAL_WRITE_FAILED", failure_phase="journal")
            if isinstance(exc, ToolError):
                outcome.update(error_code=exc.code, failure_phase="verification")
            journal.finish_run(outcome, f"run.{status}")
