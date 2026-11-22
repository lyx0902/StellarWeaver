"""Single local worker for durable workbench jobs."""

from collections import deque
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine

from stellarweaver.domain import JSONDict
from stellarweaver.tools.workspace import ToolError
from stellarweaver.web import db, local_accounts
from stellarweaver.web.execution import execute
from stellarweaver.web.isolation import DockerRunner, Isolation, saved_worktree, task_worktree
from stellarweaver.web.journal import PgJournal
from stellarweaver.web.remote_runner import RemoteRunner


def recover_interrupted(engine: Engine, tenant: str, *, include_claimed: bool) -> None:
    with db.scoped(engine, tenant) as conn:
        runs = (
            conn.execute(
                text("""
            SELECT r.id,r.session_id, j.state='running' AS claimed
            FROM sw.runs r LEFT JOIN sw.run_jobs j ON (r.tenant_id,r.id)=(j.tenant_id,j.run_id)
            WHERE r.status='active' AND (j.run_id IS NULL OR (:recover_jobs AND j.state='running'))
            FOR UPDATE OF r
        """),
                {"recover_jobs": include_claimed},
            )
            .mappings()
            .all()
        )
        for row in runs:
            journal = PgJournal(
                engine, tenant, str(row["session_id"]), str(row["id"]), Path("."), Path(".")
            )
            journal.interrupt(
                conn, "WORKER_INTERRUPTED" if row["claimed"] else "SERVER_INTERRUPTED"
            )


def run_worker(
    engine: Engine,
    tenant: str,
    workspaces: Mapping[str, Path],
    state_dir: Path,
    *,
    provider: str,
    api_key: str | None = None,
    enable_repair: bool = False,
    once: bool = False,
    shutdown: Event | None = None,
    isolation: Isolation | None = None,
) -> None:
    if provider not in {"fake", "deepseek"} or (provider == "deepseek" and not api_key):
        raise ValueError("Choose FakeModel or configure the DeepSeek key")
    if any(root.is_relative_to(state_dir) for root in workspaces.values()):
        raise ValueError("Web state directory cannot contain a registered workspace")
    owner = str(uuid4())
    stop = shutdown if shutdown is not None else Event()
    with db.service_lock(engine, queued=True, worker=True) as lock:
        if isolation is not None:
            isolation.reap()
        recover_interrupted(engine, tenant, include_claimed=True)
        while not stop.is_set():
            job = db.claim(engine, tenant, owner, lease=lock)
            if job is None:
                if once:
                    return
                stop.wait(0.25)
                continue
            _execute_claimed(
                engine,
                tenant,
                workspaces,
                state_dir,
                provider,
                api_key,
                enable_repair,
                stop,
                isolation,
                owner,
                job,
            )
            if once:
                return


def _execute_claimed(
    engine: Engine,
    tenant: str,
    workspaces: Mapping[str, Path],
    state_dir: Path,
    provider: str,
    api_key: str | None,
    enable_repair: bool,
    stop: Event,
    isolation: Isolation | None,
    owner: str,
    job: JSONDict,
    remote: tuple[str, str] | None = None,
    local: local_accounts.LocalConfig | None = None,
) -> None:
    root = workspaces.get(job["workspace_id"])
    chat = False
    setup_error = None
    if local is not None and job["member_id"]:
        project = local_accounts.project_for_job(
            engine, tenant, job["member_id"], job["workspace_id"]
        )
        if project is not None:
            chat = project["relative_path"] is None
            try:
                if chat:
                    root = state_dir / "chat" / tenant / job["session_id"]
                    root.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    root = local.folder(project["relative_path"])
            except (OSError, ValueError):
                setup_error = "WORKSPACE_UNAVAILABLE"
    repair = job["mode"] in db.REPAIR_MODES
    skill_project = (
        root
        if not chat
        and root is not None
        and db.project_skill_trust(engine, tenant, job["member_id"], job["workspace_id"])
        else None
    )
    configured = (
        root is not None
        and root.is_dir()
        and root.resolve() == root
        and (not repair or (enable_repair and not chat))
        and job["provider"] == provider
        and job["isolated"] == (isolation is not None and not chat)
    )
    if (
        configured
        and isolation is not None
        and root is not None
        and not chat
        and setup_error is None
    ):
        try:
            if job["review"] is not None:
                root = saved_worktree(state_dir, tenant, job["review"]["session_id"])
            root = (
                saved_worktree(state_dir, tenant, job["session_id"])
                if job["continuation"] is not None
                else task_worktree(state_dir, tenant, job["session_id"], root)
            )
        except ToolError as exc:
            setup_error = exc.code
        except (OSError, ValueError):
            setup_error = "WORKTREE_SETUP_FAILED"
    journal = PgJournal(
        engine,
        tenant,
        job["session_id"],
        job["run_id"],
        root or Path("."),
        state_dir,
        repair=repair and configured and setup_error is None,
        claim=(owner, job["attempt"]),
        isolated=job["isolated"],
        member_id=job["member_id"],
        metered=provider == "deepseek",
        chat=chat,
    )
    journal.append_event("run.claimed", {})
    if not configured or setup_error is not None:
        journal.finish_run(
            {
                "run_status": "failed",
                "verification_status": "not_run",
                "error_code": setup_error or "WORKER_CONFIGURATION_MISMATCH",
            },
            "run.failed",
        )
    else:
        runner: DockerRunner | RemoteRunner | None = None
        if root is not None and isolation is not None and not chat:
            runner = (
                RemoteRunner(root, isolation, *remote) if remote else DockerRunner(root, isolation)
            )
        execute(
            journal,
            job["prompt"],
            stop,
            provider,
            api_key,
            90,
            runner,
            job["continuation"],
            job["review"],
            auto=job["mode"] == "auto",
            selected_skills=job["skills"],
            skill_project=skill_project,
            undo=job["undo"],
            context_window_tokens=job["context_window_tokens"],
        )


def run_team_worker(
    engine: Engine,
    workspaces: Mapping[str, Mapping[str, Path]],
    state_dir: Path,
    *,
    provider: str,
    api_key: str | None,
    enable_repair: bool,
    isolation: Isolation,
    remote: tuple[str, str],
    slots: int,
    shutdown: Event,
    once: bool = False,
    local: local_accounts.LocalConfig | None = None,
) -> None:
    if (
        not 1 <= slots <= 8
        or (not workspaces and local is None)
        or (provider == "deepseek" and not api_key)
    ):
        raise ValueError("Invalid shared worker configuration")
    if any(
        root.is_relative_to(state_dir) for paths in workspaces.values() for root in paths.values()
    ):
        raise ValueError("Web state directory cannot contain a registered workspace")
    order = deque(workspaces)
    owner = str(uuid4())
    running: set[Future[None]] = set()
    next_refresh = 0.0
    with db.service_lock(engine, queued=True, worker=True) as lease:
        for tenant in order:
            recover_interrupted(engine, tenant, include_claimed=True)
        with ThreadPoolExecutor(
            max_workers=slots, thread_name_prefix="stellarweaver-job"
        ) as workers:
            try:
                while not shutdown.is_set():
                    if local is not None and monotonic() >= next_refresh:
                        for tenant in local_accounts.tenants(engine):
                            if tenant not in order:
                                recover_interrupted(engine, tenant, include_claimed=True)
                                order.append(tenant)
                        next_refresh = monotonic() + 1
                    for future in tuple(running):
                        if future.done():
                            running.remove(future)
                            future.result()
                    if len(running) < slots:
                        for _ in range(len(order)):
                            tenant = order[0]
                            order.rotate(-1)
                            job = db.claim(engine, tenant, owner, lease=lease, one_per_member=True)
                            if job is None:
                                continue
                            future = workers.submit(
                                _execute_claimed,
                                engine,
                                tenant,
                                workspaces.get(tenant, {}),
                                state_dir,
                                provider,
                                api_key,
                                enable_repair,
                                shutdown,
                                isolation,
                                owner,
                                job,
                                remote,
                                local,
                            )
                            if once:
                                future.result()
                                return
                            running.add(future)
                            break
                        else:
                            if once:
                                return
                            shutdown.wait(0.15)
                    else:
                        shutdown.wait(0.10)
            finally:
                shutdown.set()
