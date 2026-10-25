"""Small CLI composition root for the explicit single-agent runtime."""

import asyncio
import json
import os
import shutil
import sys
import time
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

import typer
from prompt_toolkit import PromptSession
from pydantic import ValidationError
from rich.console import Console

from stellarweaver import __version__
from stellarweaver.changes import ChangeObservationUnknown, ChangesManager
from stellarweaver.config import AppConfig, env_file_path, get_api_key, load_config
from stellarweaver.context import ContextBudgetExceeded, ContextManager
from stellarweaver.domain import Message, ModelRequest, RunOutcome
from stellarweaver.extensions import ExtensionManager
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.base import ModelProvider, ProviderError
from stellarweaver.providers.fake import FakeModel
from stellarweaver.providers.openai_compatible import OpenAICompatibleChatProvider
from stellarweaver.runtime.budget import finish_cleanup
from stellarweaver.runtime.loop import Agent
from stellarweaver.session.store import SCHEMA_VERSION, SessionBusy, SessionStore
from stellarweaver.session.views import render_jsonl, render_markdown
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.ui.chat import ChatLoop
from stellarweaver.ui.console import TerminalUI

app = typer.Typer(no_args_is_help=True, help="StellarWeaver: an explicit local coding agent.")
console = Console(stderr=True)
ProviderName = Literal["fake", "deepseek", "openai-compatible-chat"]
PolicyName = Literal["ask", "read-only", "workspace-edit"]


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"stellarweaver {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version."),
    ] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable terminal colors.")] = False,
) -> None:
    if no_color:
        os.environ["NO_COLOR"] = "1"
        console.no_color = True


@app.command("version")
def version_command() -> None:
    """Show the installed package version."""
    typer.echo(f"stellarweaver {__version__}")
    typer.echo(f"Python {sys.version.split()[0]}; supported session schema: {SCHEMA_VERSION}")


def build_provider(
    config: AppConfig,
    workspace: Path,
    provider: ProviderName | None,
    script: Path | None,
    env_file: Path | None = None,
    stream: bool | None = None,
) -> ModelProvider:
    selected = provider or config.model.provider
    if selected == "fake":
        return FakeModel.from_script(script) if script is not None else FakeModel()
    if script is not None:
        raise ValueError("--script requires --provider fake")
    overrides: dict[str, object] = {"provider": selected}
    if stream is not None:
        overrides["supports_streaming"] = stream
    model = config.model.model_copy(update=overrides)
    key = get_api_key(model, workspace, env_file)
    if key is None:
        raise ValueError(
            f"API key missing. Fill {env_file_path(workspace, env_file)} with {model.api_key_env}=your_key; "
            "use --provider fake for offline execution."
        )
    return OpenAICompatibleChatProvider(model, key)


def effective_config(
    config: AppConfig,
    provider: ProviderName | None,
    policy: PolicyName | None,
    stream: bool | None,
) -> AppConfig:
    """Include explicit CLI overrides in the reproducible credential-free snapshot."""
    model = config.model.model_copy(
        update={
            "provider": provider or config.model.provider,
            **({"supports_streaming": stream} if stream is not None else {}),
        }
    )
    permissions = config.policy.model_copy(update={"preset": policy or config.policy.preset})
    return config.model_copy(update={"model": model, "policy": permissions})


@asynccontextmanager
async def task_extensions(
    registry: ToolRegistry,
    store: SessionStore,
    ui: TerminalUI,
    config_path: Path | None,
    trusted: bool,
    skill_dirs: tuple[Path, ...],
) -> AsyncIterator[None]:
    async with ExtensionManager(
        registry,
        store,
        config_path=config_path,
        trusted=trusted,
        user_skill_dirs=skill_dirs,
        on_event=ui.event,
    ):
        yield


async def chat_setup(
    changes: ChangesManager,
    manager: ExtensionManager,
    store: SessionStore,
    ui: TerminalUI,
    max_wall_seconds: float,
) -> RunOutcome | None:
    """Bound session initialization without charging user thinking to task runs."""
    started = time.monotonic()
    ui.event(store.append_event("session.setup.started", {"max_wall_seconds": max_wall_seconds}))
    status: Literal["budget_exceeded", "cancelled", "failed"] | None = None
    failure_reason = ""
    deadline = asyncio.timeout(max_wall_seconds)
    try:
        async with deadline:
            await changes.capture_baseline()
            if time.monotonic() - started >= max_wall_seconds:
                raise TimeoutError
            await manager.__aenter__()
    except TimeoutError:
        status = "budget_exceeded"
    except ChangeObservationUnknown as exc:
        status = "cancelled" if exc.status == "cancelled" else "failed"
        failure_reason = ": " + str(exc)
    except asyncio.CancelledError:
        status = "cancelled"
    if status is None and (deadline.expired() or time.monotonic() - started >= max_wall_seconds):
        status = "budget_exceeded"
    if status is not None:
        cleanup_reason = ""
        try:
            interrupted = await finish_cleanup(manager.aclose())
            if interrupted:
                status = "cancelled"
        except TimeoutError:
            cleanup_reason = "; cleanup grace expired; resource state unconfirmed"
            ui.event(
                store.append_event(
                    "session.setup.cleanup_failed",
                    {
                        "error_type": "TimeoutError",
                        "resource_state": "unconfirmed",
                    },
                )
            )
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as exc:
            cleanup_reason = f"; cleanup failed: {type(exc).__name__}; resource state unconfirmed"
            ui.event(
                store.append_event(
                    "session.setup.cleanup_failed",
                    {
                        "error_type": type(exc).__name__,
                        "resource_state": "unconfirmed",
                    },
                )
            )
        outcome = RunOutcome(
            status,
            "unknown",
            "not_run",
            reason="chat session setup " + status + failure_reason + cleanup_reason,
            duration_ms=int((time.monotonic() - started) * 1000),
            session_id=store.session_id,
            run_id=store.run_id,
            state_path=str(store.path),
        )
        store.finish(asdict(outcome))
        ui.event(store.append_event("run." + status, asdict(outcome)))
        ui.outcome(outcome)
        return outcome
    ui.event(
        store.append_event(
            "session.setup.completed",
            {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "task_budget_charged": False,
            },
        )
    )
    return None


async def execute_task(
    task: str,
    workspace: Path,
    config_path: Path | None,
    provider: ProviderName | None,
    script: Path | None,
    policy: PolicyName | None,
    policy_file: Path | None,
    json_output: bool,
    interactive: bool,
    data_dir: Path | None,
    fail_on_verification_error: bool,
    env_file: Path | None = None,
    stream: bool | None = None,
    extensions: Path | None = None,
    trust_workspace: bool = False,
    skill_dirs: tuple[Path, ...] = (),
) -> int:
    if not task.strip():
        raise ValueError("Task must not be empty")
    workspace = await asyncio.to_thread(workspace.resolve, strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    config = effective_config(load_config(workspace, config_path), provider, policy, stream)
    model = build_provider(config, workspace, provider, script, env_file, stream=stream)
    ui = TerminalUI(json_output=json_output)
    try:
        with SessionStore(workspace, data_dir=data_dir) as store:
            store.set_config(config.model_dump())
            engine = PolicyEngine(
                policy or config.policy.preset,
                workspace=workspace,
                interactive=interactive and sys.stdin.isatty() and sys.stdout.isatty(),
                approval=ui.approve,
                policy_file=policy_file,
            )
            registry = ToolRegistry(
                workspace,
                engine,
                execution=config.execution,
                artifacts_dir=store.artifacts_dir,
            )
            agent = Agent(
                model,
                registry,
                limits=config.limits,
                context_config=config.context,
                preflight=lambda: task_extensions(
                    registry, store, ui, extensions, trust_workspace, skill_dirs
                ),
                store=store,
                on_event=ui.event,
                on_delta=ui.delta,
                stream=stream is not False,
                max_retries=config.retry.max_retries,
                retry_base_seconds=config.retry.base_delay_seconds,
                max_retry_delay_seconds=config.retry.max_delay_seconds,
                max_output_tokens=config.model.max_output_tokens,
            )
            outcome = await agent.run(task)
            ui.outcome(outcome)
            return outcome_exit_code(outcome, fail_on_verification_error)
    finally:
        if isinstance(model, OpenAICompatibleChatProvider):
            await model.aclose()


def outcome_exit_code(outcome: RunOutcome, fail_on_verification_error: bool = False) -> int:
    statuses = {"blocked": 3, "budget_exceeded": 4, "cancelled": 130, "failed": 1}
    if outcome.run_status in statuses:
        return statuses[outcome.run_status]
    return 5 if fail_on_verification_error and outcome.verification_status == "failed" else 0


async def execute_chat(
    workspace: Path,
    config_path: Path | None,
    provider: ProviderName | None,
    script: Path | None,
    policy: PolicyName | None,
    policy_file: Path | None,
    data_dir: Path | None,
    env_file: Path | None = None,
    stream: bool | None = None,
    extensions: Path | None = None,
    trust_workspace: bool = False,
    skill_dirs: tuple[Path, ...] = (),
) -> int:
    workspace = await asyncio.to_thread(workspace.resolve, strict=True)
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    config = effective_config(load_config(workspace, config_path), provider, policy, stream)
    model = build_provider(config, workspace, provider, script, env_file, stream=stream)
    ui = TerminalUI()
    last: RunOutcome | None = None
    try:
        with SessionStore(workspace, data_dir=data_dir) as store:
            store.set_config(config.model_dump())
            engine = PolicyEngine(
                policy or config.policy.preset,
                workspace=workspace,
                interactive=True,
                approval=ui.approve,
                policy_file=policy_file,
            )
            registry = ToolRegistry(
                workspace, engine, execution=config.execution, artifacts_dir=store.artifacts_dir
            )
            agent = Agent(
                model,
                registry,
                limits=config.limits,
                context_config=config.context,
                store=store,
                on_event=ui.event,
                on_delta=ui.delta,
                stream=stream is not False,
                max_retries=config.retry.max_retries,
                retry_base_seconds=config.retry.base_delay_seconds,
                max_retry_delay_seconds=config.retry.max_delay_seconds,
                max_output_tokens=config.model.max_output_tokens,
            )
            changes = ChangesManager(workspace, store, engine, on_event=ui.event)
            build_preset = engine.preset

            async def run(task: str, continued: bool) -> RunOutcome:
                nonlocal last
                # A task can be cancelled immediately after create_task, before
                # this callback or Agent.run starts. Actual context determines
                # whether there is a prior turn to continue.
                del continued
                last = await agent.continue_run(task) if agent.messages else await agent.run(task)
                return last

            def status() -> str:
                usage = (
                    f"Last task: {last.run_status}; requests {last.model_calls}; "
                    f"tools {last.tool_calls}; input/output tokens "
                    f"{last.input_tokens}/{last.output_tokens}. "
                    if last is not None
                    else "No task submitted. "
                )
                return (
                    f"Workspace: {workspace}\n"
                    f"Provider: {provider or config.model.provider}; model: {config.model.model}; "
                    f"backend: {config.execution.backend}; policy: {engine.preset}\n"
                    f"Session: {store.session_id}; evidence: {store.path}\n"
                    f"Per-task limits: {config.limits.max_model_calls} requests, "
                    f"{config.limits.max_tool_calls} tools, {config.limits.max_wall_seconds}s. "
                    + usage
                )

            async def command(name: str, argument: str) -> str:
                nonlocal model, config, last
                try:
                    if name == "/model":
                        if not argument:
                            return f"Provider: {config.model.provider}; model: {config.model.model}"
                        if config.model.provider == "fake":
                            raise ValueError(
                                "FakeModel has no named remote model; start a real provider to switch models"
                            )
                        if len(argument) > 200 or any(
                            character.isspace() for character in argument
                        ):
                            raise ValueError("Model name must be one nonempty identifier")
                        next_model_config = config.model.model_copy(update={"model": argument})
                        next_config = config.model_copy(update={"model": next_model_config})
                        candidate = build_provider(
                            next_config, workspace, None, None, env_file, stream=stream
                        )
                        try:
                            capability = candidate.capabilities()
                            if not capability.tool_calling:
                                raise ValueError(
                                    "Selected model configuration does not support tool calling"
                                )
                            window = capability.context_window_tokens
                            if (
                                window is None
                                or window
                                <= config.model.max_output_tokens + config.context.reserve_tokens
                            ):
                                raise ValueError(
                                    "Selected model has no usable configured context budget"
                                )
                            projection = ContextManager().build(
                                agent.projected_history(),
                                registry.schemas(),
                                config.model.max_output_tokens,
                                window,
                                config.context,
                            )
                        except BaseException:
                            if isinstance(candidate, OpenAICompatibleChatProvider):
                                await candidate.aclose()
                            raise
                        previous = model
                        model, agent.provider, config = candidate, candidate, next_config
                        store.set_config(config.model_dump())
                        store.append_event(
                            "model.changed",
                            {
                                "provider": config.model.provider,
                                "model": config.model.model,
                                "context_window_tokens": window,
                                "projected_input_estimate": projection.estimated_tokens,
                                "capability_source": "configured_adapter",
                            },
                        )
                        if isinstance(previous, OpenAICompatibleChatProvider):
                            await previous.aclose()
                        return f"Selected {config.model.model}; configured capabilities revalidated. Connectivity is checked on the next request."
                    if name == "/plan":
                        if argument not in {"", "build", "read-only"}:
                            raise ValueError(
                                "Use /plan for read-only analysis or /plan build to restore initial policy"
                            )
                        engine.preset = build_preset if argument == "build" else "read-only"
                        config = config.model_copy(
                            update={
                                "policy": config.policy.model_copy(update={"preset": engine.preset})
                            }
                        )
                        store.set_config(config.model_dump())
                        store.append_event("policy.mode_changed", {"preset": engine.preset})
                        return f"Policy: {engine.preset}. Existing scoped grants are unchanged."
                    if name == "/skill":
                        if not argument:
                            return "Use /skill NAME. Available skills: " + ", ".join(
                                str(item["name"]) for item in extension_manager.skill_catalog
                            )
                        result = await agent.load_skill(argument)
                        return f"Skill {argument}: {result.status}\n{result.content}"
                    if argument:
                        raise ValueError(f"{name} takes no arguments")
                    if name == "/diff":
                        return await changes.diff()
                    if name == "/undo":
                        return json.dumps(await changes.undo(), ensure_ascii=False, indent=2)
                    if name == "/compact":
                        return json.dumps(await agent.compact(), ensure_ascii=False, indent=2)
                    if name == "/clear":
                        report = await agent.clear_context()
                        last = None
                        return json.dumps(report, ensure_ascii=False, indent=2)
                    return "Unknown command. Use /help."
                except ChangeObservationUnknown as exc:
                    return f"Command {exc.status}: {exc}"
                except (OSError, ValueError, ProviderError, ContextBudgetExceeded) as exc:
                    return f"Command failed ({type(exc).__name__}): {exc}"

            extension_manager = ExtensionManager(
                registry,
                store,
                config_path=extensions,
                trusted=trust_workspace,
                user_skill_dirs=skill_dirs,
                on_event=ui.event,
            )
            setup_outcome = await chat_setup(
                changes, extension_manager, store, ui, config.limits.max_wall_seconds
            )
            if setup_outcome is not None:
                return outcome_exit_code(setup_outcome)
            try:
                prompts: PromptSession[str] = PromptSession()
                return await ChatLoop(prompts.prompt_async, run, ui, status, command=command).run()
            finally:
                await finish_cleanup(extension_manager.aclose())
    finally:
        if isinstance(model, OpenAICompatibleChatProvider):
            await model.aclose()


def run_safely(*, chat: bool = False, resume: bool = False, **kwargs: object) -> None:
    try:
        if resume:
            operation = execute_resume(**kwargs)  # type: ignore[arg-type]
        else:
            operation = execute_chat(**kwargs) if chat else execute_task(**kwargs)  # type: ignore[arg-type]
        code = asyncio.run(operation)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_context=False)
        )
        console.print(f"Configuration or script validation failed: {details}", markup=False)
        raise typer.Exit(2) from None
    except SessionBusy as exc:
        console.print(f"Session is in use: {exc}", markup=False)
        raise typer.Exit(3) from None
    except (OSError, ValueError, tomllib.TOMLDecodeError, ProviderError) as exc:
        console.print(f"{type(exc).__name__}: {exc}", markup=False)
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    raise typer.Exit(code)


async def execute_resume(
    session_id: str,
    task: str | None,
    workspace: Path | None,
    config_path: Path | None,
    provider: ProviderName | None,
    script: Path | None,
    policy: PolicyName | None,
    policy_file: Path | None,
    json_output: bool,
    data_dir: Path | None,
    fail_on_verification_error: bool,
    env_file: Path | None = None,
    stream: bool | None = None,
    extensions: Path | None = None,
    trust_workspace: bool = False,
    skill_dirs: tuple[Path, ...] = (),
) -> int:
    """Acquire the existing writer and rebuild current policy before continuation."""
    if task is not None and not task.strip():
        raise ValueError("Task must not be empty")
    snapshot = SessionStore.inspect(session_id, workspace=workspace, data_dir=data_dir)
    root = snapshot.workspace.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Recorded workspace must be a directory")
    config = effective_config(load_config(root, config_path), provider, policy, stream)
    ui = TerminalUI(json_output=json_output)
    with SessionStore.open(session_id, workspace=root, data_dir=data_dir) as store:
        store.set_config(config.model_dump())
        model = build_provider(config, root, provider, script, env_file, stream=stream)
        try:
            engine = PolicyEngine(
                policy or config.policy.preset,
                workspace=root,
                interactive=sys.stdin.isatty() and sys.stdout.isatty() and not json_output,
                approval=ui.approve,
                policy_file=policy_file,
            )
            registry = ToolRegistry(
                root, engine, execution=config.execution, artifacts_dir=store.artifacts_dir
            )
            agent = Agent(
                model,
                registry,
                limits=config.limits,
                context_config=config.context,
                preflight=lambda: task_extensions(
                    registry, store, ui, extensions, trust_workspace, skill_dirs
                ),
                store=store,
                on_event=ui.event,
                on_delta=ui.delta,
                stream=stream is not False,
                max_retries=config.retry.max_retries,
                retry_base_seconds=config.retry.base_delay_seconds,
                max_retry_delay_seconds=config.retry.max_delay_seconds,
                max_output_tokens=config.model.max_output_tokens,
            )
            outcome = await agent.resume(task)
            ui.outcome(outcome)
            return outcome_exit_code(outcome, fail_on_verification_error)
        finally:
            if isinstance(model, OpenAICompatibleChatProvider):
                await model.aclose()


sessions_app = typer.Typer(no_args_is_help=True, help="Inspect recorded local sessions.")
app.add_typer(sessions_app, name="sessions")


@sessions_app.command("list")
def sessions_list(
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit a JSON array.")] = False,
) -> None:
    """List session metadata without opening a provider or modifying journals."""
    try:
        sessions = SessionStore.list_sessions(data_dir=data_dir, workspace=workspace)
        if json_output:
            typer.echo(json.dumps(sessions, ensure_ascii=True))
        elif not sessions:
            typer.echo("No recorded sessions.")
        else:
            for session in sessions:
                typer.echo(
                    f"{session['session_id']}  {session['status']}  "
                    f"{session['workspace']}  {session.get('updated_at', '')}"
                )
    except (OSError, ValueError) as exc:
        console.print(f"{type(exc).__name__}: {exc}", markup=False)
        raise typer.Exit(2) from None


@app.command("replay")
def replay_command(
    session_id: Annotated[str, typer.Argument(help="Session ID, directory or SQLite path.")],
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render historical JSONL records.")
    ] = False,
) -> None:
    """Render recorded evidence offline. Never call models, tools or recovery."""
    try:
        snapshot = SessionStore.inspect(session_id, workspace=workspace, data_dir=data_dir)
        typer.echo(render_jsonl(snapshot) if json_output else render_markdown(snapshot), nl=False)
    except (OSError, ValueError) as exc:
        console.print(f"{type(exc).__name__}: {exc}", markup=False)
        raise typer.Exit(2) from None


@app.command("export")
def export_command(
    session_id: Annotated[str, typer.Argument(help="Session ID, directory or SQLite path.")],
    format: Annotated[
        Literal["markdown", "jsonl"], typer.Option("--format", help="Static export format.")
    ] = "markdown",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Create a new UTF-8 file; never overwrite."),
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Export static source-labelled messages, operations, events and outcomes."""
    try:
        snapshot = SessionStore.inspect(session_id, workspace=workspace, data_dir=data_dir)
        content = render_jsonl(snapshot) if format == "jsonl" else render_markdown(snapshot)
        if output is None:
            typer.echo(content, nl=False)
        else:
            with output.open("x", encoding="utf-8", newline="\n") as destination:
                destination.write(content)
            console.print(f"Exported historical session to {output.resolve()}", markup=False)
    except (OSError, ValueError) as exc:
        console.print(f"{type(exc).__name__}: {exc}", markup=False)
        raise typer.Exit(2) from None


@app.command("resume")
def resume_command(
    session_id: Annotated[
        str, typer.Argument(help="Existing session ID, directory or SQLite path.")
    ],
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="Start an explicit new task using saved context.")
    ] = None,
    workspace: Annotated[Path | None, typer.Option("--workspace", "-w")] = None,
    config: Annotated[Path | None, typer.Option("--config")] = None,
    provider: Annotated[ProviderName | None, typer.Option()] = None,
    script: Annotated[Path | None, typer.Option(help="Offline FakeModel reply script.")] = None,
    policy: Annotated[PolicyName | None, typer.Option()] = None,
    policy_file: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    fail_on_verification_error: Annotated[bool, typer.Option()] = False,
    env_file: Annotated[Path | None, typer.Option(help="Credential .env file.")] = None,
    stream: Annotated[
        bool | None, typer.Option("--stream/--no-stream", help="Override streaming support.")
    ] = None,
    extensions: Annotated[
        Path | None,
        typer.Option(
            "--extensions", help="Explicit extension configuration.", rich_help_panel="Extensions"
        ),
    ] = None,
    trust_workspace: Annotated[
        bool,
        typer.Option(
            "--trust-workspace",
            help="Trust project skills and permit authorized extension startup.",
            rich_help_panel="Extensions",
        ),
    ] = False,
    skill_dir: Annotated[
        list[Path] | None,
        typer.Option(
            "--skill-dir",
            help="Explicit user skill directory; repeat to add more.",
            rich_help_panel="Extensions",
        ),
    ] = None,
) -> None:
    """Safely continue a saved session under current configuration and permissions."""
    run_safely(
        resume=True,
        session_id=session_id,
        task=prompt,
        workspace=workspace,
        config_path=config,
        provider=provider,
        script=script,
        policy=policy,
        policy_file=policy_file,
        json_output=json_output,
        data_dir=data_dir,
        fail_on_verification_error=fail_on_verification_error,
        env_file=env_file,
        stream=stream,
        extensions=extensions,
        trust_workspace=trust_workspace,
        skill_dirs=tuple(skill_dir or ()),
    )


@app.command("exec")
def exec_command(
    task: Annotated[str | None, typer.Argument(help="Task for the coding agent.")] = None,
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="Task text (alternative to positional task).")
    ] = None,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-w", help="Fixed workspace root.")
    ] = Path("."),
    config: Annotated[
        Path | None, typer.Option("--config", help="Explicit TOML configuration.")
    ] = None,
    provider: Annotated[
        ProviderName | None, typer.Option(help="fake is offline; deepseek uses your key.")
    ] = None,
    script: Annotated[Path | None, typer.Option(help="JSON array of FakeModel replies.")] = None,
    policy: Annotated[
        PolicyName | None, typer.Option(help="Program-owned authorization preset.")
    ] = None,
    policy_file: Annotated[Path | None, typer.Option(help="Explicit scoped JSON grants.")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit events and outcome as JSON Lines.")
    ] = False,
    data_dir: Annotated[
        Path | None, typer.Option(help="Session data directory (default outside workspace).")
    ] = None,
    fail_on_verification_error: Annotated[
        bool, typer.Option(help="Exit 5 if real verification failed.")
    ] = False,
    env_file: Annotated[
        Path | None, typer.Option(help="Credential .env file (default launcher .env).")
    ] = None,
    stream: Annotated[
        bool | None,
        typer.Option(
            "--stream/--no-stream",
            help="Override configured streaming support; default respects provider capabilities.",
        ),
    ] = None,
    extensions: Annotated[
        Path | None,
        typer.Option(
            "--extensions", help="Explicit extension configuration.", rich_help_panel="Extensions"
        ),
    ] = None,
    trust_workspace: Annotated[
        bool,
        typer.Option(
            "--trust-workspace",
            help="Trust project skills and permit authorized extension startup.",
            rich_help_panel="Extensions",
        ),
    ] = False,
    skill_dir: Annotated[
        list[Path] | None,
        typer.Option(
            "--skill-dir",
            help="Explicit user skill directory; repeat to add more.",
            rich_help_panel="Extensions",
        ),
    ] = None,
) -> None:
    """Execute one task without interactive authorization; missing grants stop the run."""
    if (task is None) == (prompt is None):
        raise typer.BadParameter("Provide exactly one positional task or --prompt TEXT")
    run_safely(
        task=task if task is not None else prompt,
        workspace=workspace,
        config_path=config,
        provider=provider,
        script=script,
        policy=policy,
        policy_file=policy_file,
        json_output=json_output,
        interactive=False,
        data_dir=data_dir,
        fail_on_verification_error=fail_on_verification_error,
        env_file=env_file,
        stream=stream,
        extensions=extensions,
        trust_workspace=trust_workspace,
        skill_dirs=tuple(skill_dir or ()),
    )


@app.command("chat")
def chat_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    config: Annotated[Path | None, typer.Option("--config")] = None,
    provider: Annotated[ProviderName | None, typer.Option()] = None,
    script: Annotated[Path | None, typer.Option()] = None,
    policy: Annotated[PolicyName | None, typer.Option()] = None,
    policy_file: Annotated[Path | None, typer.Option()] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    env_file: Annotated[
        Path | None, typer.Option(help="Credential .env file (default launcher .env).")
    ] = None,
    stream: Annotated[
        bool | None,
        typer.Option("--stream/--no-stream", help="Override configured streaming support."),
    ] = None,
    extensions: Annotated[
        Path | None,
        typer.Option(
            "--extensions", help="Explicit extension configuration.", rich_help_panel="Extensions"
        ),
    ] = None,
    trust_workspace: Annotated[
        bool,
        typer.Option(
            "--trust-workspace",
            help="Trust project skills and permit authorized extension startup.",
            rich_help_panel="Extensions",
        ),
    ] = False,
    skill_dir: Annotated[
        list[Path] | None,
        typer.Option(
            "--skill-dir",
            help="Explicit user skill directory; repeat to add more.",
            rich_help_panel="Extensions",
        ),
    ] = None,
) -> None:
    """Continue a conversation with TTY approvals. Use exec for automation."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print("chat requires an interactive TTY; use stellarweaver exec for automation.")
        raise typer.Exit(2)
    run_safely(
        chat=True,
        workspace=workspace,
        config_path=config,
        provider=provider,
        script=script,
        policy=policy,
        policy_file=policy_file,
        data_dir=data_dir,
        env_file=env_file,
        stream=stream,
        extensions=extensions,
        trust_workspace=trust_workspace,
        skill_dirs=tuple(skill_dir or ()),
    )


@app.command("doctor")
def doctor_command(
    offline: Annotated[
        bool, typer.Option("--offline", help="Check local setup without network access.")
    ] = False,
    check_model: Annotated[
        bool, typer.Option("--check-model", help="Explicitly send one minimal model API request.")
    ] = False,
    provider: Annotated[
        ProviderName | None, typer.Option(help="Provider to inspect or check, e.g. deepseek.")
    ] = None,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    config: Annotated[Path | None, typer.Option("--config")] = None,
    env_file: Annotated[Path | None, typer.Option(help="Credential .env file.")] = None,
) -> None:
    """Inspect local setup; network access requires the explicit --check-model flag."""
    if offline and check_model:
        raise typer.BadParameter("--offline and --check-model are mutually exclusive")
    try:
        root = workspace.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Workspace must be a directory")
        settings = load_config(root, config)
        selected = provider or settings.model.provider
        typer.echo(f"stellarweaver {__version__}; Python {sys.version.split()[0]}")
        typer.echo(f"workspace: {root}")
        typer.echo(f"git: {shutil.which('git') or 'missing'}")
        typer.echo(f"rg: {shutil.which('rg') or 'missing (Python search fallback available)'}")
        typer.echo(f"provider: {selected}; model: {settings.model.model}")
        key_status = "present" if get_api_key(settings.model, root, env_file) else "missing"
        typer.echo(f"API key: {key_status}; .env location: {env_file_path(root, env_file)}")
        if "example.com" in settings.model.base_url or "REPLACE" in settings.model.model:
            typer.echo("Placeholder model configuration must be replaced before real requests.")
            if check_model:
                raise typer.Exit(2)
        if not check_model:
            typer.echo("Offline diagnostics complete. Model connectivity: not tested.")
            return
        if selected == "fake":
            console.print(
                "FakeModel is offline. Use doctor --check-model --provider deepseek "
                "to explicitly check the real provider.",
                markup=False,
            )
            raise typer.Exit(2)
        if key_status == "missing":
            console.print("API key missing. Fill the .env location printed above.", markup=False)
            raise typer.Exit(2)
        asyncio.run(check_model_connection(settings, root, selected, env_file))
    except ProviderError as exc:
        console.print(f"Model connectivity check failed: {exc}", markup=False)
        raise typer.Exit(2) from None
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        # Validation errors may contain raw input; report their type only here.
        console.print(f"Local configuration check failed ({type(exc).__name__}).")
        raise typer.Exit(2) from None
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


async def check_model_connection(
    config: AppConfig, workspace: Path, provider: ProviderName, env_file: Path | None
) -> None:
    """One explicitly requested, bounded request; never invoke local tools."""
    model = build_provider(config, workspace, provider, None, env_file, stream=False)
    marker = "STELLARWEAVER_DEEPSEEK_OK"
    try:
        response = await model.complete(
            ModelRequest(
                messages=(
                    Message(
                        "system",
                        "This is a connectivity check. Return only the exact text requested.",
                    ),
                    Message("user", "Reply exactly: " + marker),
                ),
                tools=(),
                max_output_tokens=64,
            )
        )
        if (
            response.text.strip() != marker
            or response.tool_calls
            or response.stop_reason not in {"stop", "end_turn"}
        ):
            known_reason = (
                response.stop_reason
                if response.stop_reason
                in {"stop", "end_turn", "length", "tool_calls", "content_filter"}
                else "other"
            )
            raise ProviderError(
                "Model API responded, but the expected complete marker was not returned "
                f"(stop_reason={known_reason}; visible_text_length={len(response.text)})."
            )
        typer.echo(f"Model connectivity: connected ({provider}/{config.model.model}).")
        typer.echo(f"Marker: {marker}")
        if response.usage is not None:
            usage = {
                name: response.usage[name]
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")
                if isinstance(response.usage.get(name), int)
            }
            typer.echo(f"Reported token usage: {usage}")
    finally:
        if isinstance(model, OpenAICompatibleChatProvider):
            await model.aclose()
