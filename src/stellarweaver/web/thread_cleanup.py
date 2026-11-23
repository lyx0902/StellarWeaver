"""Purge deleted threads from the journal and worker's private state."""

import logging
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Engine

from stellarweaver.web import db

_LOG = logging.getLogger(__name__)
_RUN_CONTENT = ("approvals", "operations", "checkpoints", "events", "messages", "run_jobs")


def _retry_readonly_unlink(remove: Callable[[str], object], name: str, error: BaseException) -> None:
    path = Path(name)
    if (
        os.name != "nt"
        or not isinstance(error, PermissionError)
        or remove is not os.unlink
        or path.resolve() != path
        or path.stat().st_mode & stat.S_IWRITE
    ):
        raise error
    # Git marks loose objects read-only on Windows, even in a private worktree.
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    remove(name)


def _remove_state_tree(root: Path, path: Path) -> None:
    if not path.is_relative_to(root) or path == root or path.parent.resolve() != path.parent:
        raise OSError("Session state path escapes the state directory")
    if path.is_symlink():
        path.unlink()
    elif path.is_junction():
        path.rmdir()
    elif path.exists():
        shutil.rmtree(path, onexc=_retry_readonly_unlink)


def purge_deleted_thread(engine: Engine, state_dir: Path, tenant: str) -> None:
    root = state_dir.resolve()
    tenant = str(UUID(tenant))
    with db.scoped(engine, tenant) as conn:
        session = conn.execute(
            text("""
                SELECT id::text FROM sw.sessions
                WHERE deleted_at IS NOT NULL AND purged_at IS NULL AND purge_after<=now()
                ORDER BY purge_after,id LIMIT 1 FOR UPDATE SKIP LOCKED
            """)
        ).scalar_one_or_none()
        if session is None:
            return
        params = {"session": session}
        # Audit and usage still reference run IDs. Retain only run metadata needed by them.
        for table in _RUN_CONTENT:
            conn.execute(
                text(f"""
                    DELETE FROM sw.{table} AS content USING sw.runs r
                    WHERE (content.tenant_id,content.run_id)=(r.tenant_id,r.id)
                    AND r.session_id=:session
                """),
                params,
            )
        conn.execute(
            text("""
                UPDATE sw.runs SET prompt='', outcome=NULL, report=NULL, review=NULL,
                    undo=NULL, skills='[]'::jsonb
                WHERE session_id=:session
            """),
            params,
        )
        run_ids = conn.execute(
            text("SELECT id::text FROM sw.runs WHERE session_id=:session"), params
        ).scalars()
        paths = [root / tenant / str(UUID(run)) for run in run_ids]
        paths.extend(root / kind / tenant / str(UUID(session)) for kind in ("g4", "chat"))
        try:
            for path in paths:
                _remove_state_tree(root, path)
        except OSError:
            _LOG.warning("Deferred filesystem cleanup for deleted workbench session %s", session)
            conn.execute(
                text(
                    "UPDATE sw.sessions SET purge_after=now()+interval '30 seconds' WHERE id=:session"
                ),
                params,
            )
        else:
            conn.execute(
                text("UPDATE sw.sessions SET purged_at=now(),purge_after=NULL WHERE id=:session"),
                params,
            )
