"""Shared composition for CLI and browser runs, without either UI."""

from collections.abc import Callable
from pathlib import Path

from stellarweaver.config import AppConfig
from stellarweaver.policy import Approval, PolicyEngine
from stellarweaver.providers.base import ModelProvider
from stellarweaver.runtime.budget import Preflight
from stellarweaver.runtime.loop import Agent, CompletionCheck, DeltaSink, EventSink
from stellarweaver.session.journal import RunJournal
from stellarweaver.tools.commands import CommandRunner
from stellarweaver.tools.registry import ToolRegistry


def build_agent(
    config: AppConfig,
    model: ModelProvider,
    store: RunJournal,
    *,
    on_event: EventSink | None = None,
    on_delta: DeltaSink | None = None,
    interactive: bool = False,
    approval: Approval | None = None,
    policy_file: Path | None = None,
    preflight: Callable[[ToolRegistry], Preflight] | None = None,
    stream: bool = True,
    command_runner: CommandRunner | None = None,
    completion_check: CompletionCheck | None = None,
    chat_only: bool = False,
) -> Agent:
    engine = PolicyEngine(
        config.policy.preset,
        workspace=store.workspace,
        interactive=interactive,
        approval=approval,
        policy_file=policy_file,
    )
    registry = ToolRegistry(
        store.workspace,
        engine,
        execution=config.execution,
        artifacts_dir=store.artifacts_dir,
        command_runner=command_runner,
        builtins=not chat_only,
    )
    return Agent(
        model,
        registry,
        limits=config.limits,
        context_config=config.context,
        preflight=preflight(registry) if preflight else None,
        store=store,
        on_event=on_event,
        on_delta=on_delta,
        stream=stream,
        max_retries=config.retry.max_retries,
        retry_base_seconds=config.retry.base_delay_seconds,
        max_retry_delay_seconds=config.retry.max_delay_seconds,
        max_output_tokens=config.model.max_output_tokens,
        system_prompt="You are StellarWeaver. Answer the user's questions in their language. "
        "This is a conversation without a project or tools."
        if chat_only
        else None,
        project_context=not chat_only,
        completion_check=completion_check,
    )
