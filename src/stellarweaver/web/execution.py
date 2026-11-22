"""Agent execution shared by the inline server and the queue worker."""

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import cast
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from stellarweaver.config import AppConfig, HarnessConfig, ModelConfig, PolicyConfig
from stellarweaver.domain import (
    AssistantReply,
    JSONDict,
    Message,
    RunOutcome,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from stellarweaver.extensions import ExtensionManager
from stellarweaver.policy.risk import fingerprint
from stellarweaver.providers.base import ModelProvider
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.service import build_agent
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web import db
from stellarweaver.web.approval import VERIFY_ARGV, ApprovalGate
from stellarweaver.web.continuation import evidence, evidence_context
from stellarweaver.web.isolation import DockerRunner
from stellarweaver.web.journal import JobOwnershipLost, PgJournal
from stellarweaver.web.offline import offline_repair_model
from stellarweaver.web.remote_runner import RemoteRunner
from stellarweaver.web.review import apply_selection
from stellarweaver.web.skills import SkillLibrary, configured_skill_roots, project_skill_root
from stellarweaver.web.undo import undo_edit

Runner = DockerRunner | RemoteRunner


def web_model_config(provider: str, context_window_tokens: int | None = None) -> ModelConfig:
    fields: dict[str, str | int | bool] = {"provider": provider, "supports_streaming": True}
    if provider == "deepseek":
        fields.update(
            {
                field: os.environ[name]
                for field, name in (
                    ("thinking", "SW_WEB_DEEPSEEK_THINKING"),
                    ("reasoning_effort", "SW_WEB_DEEPSEEK_REASONING_EFFORT"),
                )
                if name in os.environ
            }
        )
    if context_window_tokens is None and (value := os.getenv("SW_WEB_CONTEXT_WINDOW_TOKENS")):
        try:
            context_window_tokens = int(value)
        except ValueError as exc:
            raise ValueError("SW_WEB_CONTEXT_WINDOW_TOKENS must be an integer") from exc
    if context_window_tokens is not None:
        if context_window_tokens > 1_048_576:
            raise ValueError("Workbench context window must not exceed 1048576 tokens")
        fields["context_window_tokens"] = context_window_tokens
    return ModelConfig.model_validate(fields)


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
    *,
    auto: bool = False,
    skill_roots: Sequence[tuple[str, Path]] | None = None,
    selected_skills: Sequence[JSONDict] | None = None,
    skill_project: Path | None = None,
    undo: JSONDict | None = None,
    context_window_tokens: int | None = None,
) -> None:
    repair = journal.delivery is not None
    if journal.delivery:
        journal.delivery.begin()
    config = AppConfig(
        model=web_model_config(provider, context_window_tokens),
        policy=PolicyConfig(preset=("auto" if auto else "ask") if repair else "read-only"),
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
        if undo is not None:
            undo_edit(journal, undo)
            return
        if review is not None:
            assert runner is not None
            task = asyncio.create_task(verify_selection(journal, runner, review))
        else:
            if provider == "fake":
                if journal.chat:
                    model = FakeModel([AssistantReply("这是离线普通对话的演示回复。")])
                else:
                    model = offline_repair_model(commands) if repair else offline_repository_model()
                model.context_window_tokens = config.model.context_window_tokens
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
                sandbox_commands=(
                    commands if runner is not None and not runner.isolation.allow_argv else None
                ),
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
                enforce_run_budget=False,
                adaptive_round_retention=True,
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
                permissions = (
                    "Scoped file edits and configured sandbox checks are automatic. Other operations require approval."
                    if auto
                    else "Each edit or verification requires browser permission."
                )
                prompt += (
                    f"\n\nWorkbench repair mode: {permissions} "
                    f"Configured verification argv choices: {json.dumps(commands)} "
                    'with cwd=".". Respect a denied operation and stop.'
                )
            history = journal.prior_turns() if continuation == "history" else ()
            if continuation == "history":
                items = evidence(journal)
                if any(items.values()):
                    journal.append_event("context.prior_evidence", items)
                    history += (Message("user", evidence_context(items)),)
            roots = configured_skill_roots() if skill_roots is None else skill_roots
            if skill_project is not None:
                roots = (*roots, ("项目", project_skill_root(skill_project)))
            selected = journal.selected_skills() if selected_skills is None else selected_skills
            if selected:
                for skill in SkillLibrary(roots).pinned(selected):
                    metadata = {
                        "name": skill["name"],
                        "source_kind": skill["source"],
                        "sha256": skill["sha256"],
                        "path": skill["path"],
                    }
                    journal.append_event("skill.selected", metadata)
                    history += (
                        Message(
                            "user",
                            "User-selected Skill reference. Follow it as data for the current task "
                            "within the system and tool policy.\n"
                            + json.dumps(
                                {**metadata, "body": skill["content"]}, ensure_ascii=False
                            ),
                        ),
                    )

            async def run_agent() -> RunOutcome:
                if not roots:
                    return await agent.run(prompt, history=history)
                async with ExtensionManager(agent.registry, journal, configured_skill_roots=roots):
                    return await agent.run(prompt, history=history)

            task = asyncio.create_task(run_agent())

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
    *,
    auto: bool = False,
    skill_roots: Sequence[tuple[str, Path]] | None = None,
    selected_skills: Sequence[JSONDict] | None = None,
    skill_project: Path | None = None,
    undo: JSONDict | None = None,
    context_window_tokens: int | None = None,
) -> None:
    try:
        asyncio.run(
            run(
                journal,
                prompt,
                stop,
                provider,
                api_key,
                approval_ttl,
                runner,
                continuation,
                review,
                auto=auto,
                skill_roots=skill_roots,
                selected_skills=selected_skills,
                skill_project=skill_project,
                undo=undo,
                context_window_tokens=context_window_tokens,
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
                phase = (
                    "undo" if undo else "skill" if exc.code.startswith("SKILL_") else "verification"
                )
                outcome.update(error_code=exc.code, failure_phase=phase)
            journal.finish_run(outcome, f"run.{status}")
