"""Shared composition for CLI and browser runs, without either UI."""

from collections.abc import Callable
from pathlib import Path

from stellarweaver.config import AppConfig
from stellarweaver.policy import Approval, PolicyEngine
from stellarweaver.policy.hooks import HookManager
from stellarweaver.policy.risk import fingerprint
from stellarweaver.providers.base import ModelProvider
from stellarweaver.runtime.budget import Preflight
from stellarweaver.runtime.loop import Agent, CompletionCheck, DeltaSink, EventSink
from stellarweaver.session.journal import RunJournal
from stellarweaver.tools.commands import CommandRunner
from stellarweaver.tools.development import register_development_tools
from stellarweaver.tools.navigation import register_navigation
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.staging import register_apply_patch, register_staging


def attach_harness(registry: ToolRegistry, config: AppConfig) -> None:
    if config.harness.profile == "terminal-minimal":
        return
    if config.execution.backend == "docker" and registry.command_runner is None:
        from stellarweaver.web.isolation import DockerRunner, Isolation

        assert config.execution.image is not None
        isolation = Isolation(
            config.execution.image,
            (),
            "stellarweaver-cli",
            profile=config.execution.profile,
            allow_argv=config.execution.allow_argv,
            development=config.harness.development_tools,
        )
        registry.command_runner = DockerRunner(
            registry.workspace.root,
            isolation,
            pipeline_metrics=config.harness.pipeline_metrics,
            incremental_snapshots=config.harness.incremental_sandbox_snapshots,
        )
        registry.command_boundary = fingerprint(config.execution.model_dump())
        registry.sandbox_commands = None if isolation.allow_argv else isolation.commands
        if not isolation.allow_argv:
            registry.unregister("run_command")
    if config.harness.development_tools:
        if registry.command_runner is None:
            raise ValueError("Development tools require a configured sandbox")
        register_development_tools(registry, include_linter=config.harness.include_linter)
    if config.harness.issue_navigation:
        register_navigation(registry, prefilter=config.harness.navigation_prefilter)
    if config.harness.patch_staging:
        register_staging(registry)
    if config.harness.multi_file_apply:
        register_apply_patch(registry)


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
    enable_web_search: bool = False,
    hooks: HookManager | None = None,
    command_boundary: str | None = None,
    allow_sandbox_development: bool = False,
    sandbox_commands: tuple[tuple[str, ...], ...] | None = None,
    enforce_run_budget: bool = True,
    adaptive_round_retention: bool = False,
    allow_benchmark_staging: bool = False,
    enforce_exploration_caps: bool = False,
) -> Agent:
    engine = PolicyEngine(
        config.policy.preset,
        workspace=store.workspace,
        interactive=interactive,
        approval=approval,
        policy_file=policy_file,
        allow_sandbox_development=allow_sandbox_development,
        scoped_test_grants=config.harness.scoped_test_grants,
        allow_benchmark_staging=allow_benchmark_staging and config.policy.preset == "benchmark",
    )
    registry = ToolRegistry(
        store.workspace,
        engine,
        execution=config.execution,
        artifacts_dir=store.artifacts_dir,
        command_runner=command_runner,
        builtins=not chat_only,
        hooks=hooks,
        command_boundary=command_boundary,
        sandbox_commands=sandbox_commands,
        harness=config.harness,
    )
    if not chat_only:
        attach_harness(registry, config)
    return Agent(
        model,
        registry,
        limits=config.limits,
        context_config=config.context,
        harness_config=config.harness,
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
        "This conversation has no project. If ds_search is available, use it for requested web "
        "lookups and time-sensitive facts. Link the sources you actually used."
        if chat_only
        else None,
        project_context=not chat_only,
        completion_check=completion_check,
        enable_web_search=enable_web_search,
        enforce_run_budget=enforce_run_budget,
        adaptive_round_retention=adaptive_round_retention,
        enforce_exploration_caps=enforce_exploration_caps,
    )
