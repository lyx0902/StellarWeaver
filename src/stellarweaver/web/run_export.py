"""Portable, redacted run records with a self-contained offline reader."""

import json
from html import escape
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import Connection

from stellarweaver.domain import JSONDict
from stellarweaver.web.experiment_records import redact

MAX_RECORDS = 10_000
MAX_EXPORT_BYTES = 16 * 1024 * 1024


class ExportLimitExceeded(ValueError):
    pass


def _pick(item: JSONDict, *keys: str) -> JSONDict:
    return {key: item[key] for key in keys if key in item}


def _tool_data(result: JSONDict) -> JSONDict:
    return _pick(
        result.get("data", {}),
        "path",
        "before_sha256",
        "after_sha256",
        "sha256",
        "mode",
        "exit_code",
        "code",
        "name",
        "source_kind",
        "timeout_seconds",
    )


def _event(event: JSONDict) -> JSONDict:
    body = event["payload"]
    if event["type"] == "tool.completed":
        payload = {
            **_pick(body, "tool_name", "status", "operation_id", "call_id"),
            "data": _tool_data(body),
        }
    elif event["type"] in {"assistant.completed", "run.started"}:
        payload = _pick(body, "content", "task", "request_index")
    else:
        payload = _pick(
            body,
            "status",
            "run_status",
            "error_code",
            "verification_status",
            "failure_phase",
            "tool_name",
            "tool",
            "operation_id",
            "target_operation_id",
            "path",
            "code",
            "request_index",
            "input_tokens",
            "output_tokens",
            "estimated",
            "context_window_tokens",
            "input_budget",
            "before_tokens_estimated",
            "after_tokens_estimated",
            "strategy",
            "duration_ms",
            "decision",
            "name",
            "source_kind",
            "sha256",
            "files",
            "checks",
            "skills",
        )
    return {**event, "payload": payload}


def build(conn: Connection, run: JSONDict) -> JSONDict:
    params = {"id": run["run_id"], "limit": MAX_RECORDS + 1}

    def records(query: str) -> list[JSONDict]:
        rows = conn.execute(text(query), params).mappings().all()
        if len(rows) > MAX_RECORDS:
            raise ExportLimitExceeded()
        return [dict(row) for row in rows]

    events = records("""
        SELECT event FROM sw.events WHERE run_id=:id ORDER BY seq LIMIT :limit
    """)
    messages = records("""
        SELECT position,body->>'role' AS role,
            CASE WHEN body->>'role' IN ('user','assistant') THEN left(body->>'content',32768)
            END AS content, body->>'tool_call_id' AS tool_call_id
        FROM sw.messages WHERE run_id=:id ORDER BY position LIMIT :limit
    """)
    operations = records("""
        SELECT id::text AS operation_id,call_id,tool_name,state,
            intent->>'path' AS path,decision->>'decision' AS decision,
            result - 'content' - 'artifact_ids' AS result,
            (result_event->>'seq')::bigint AS result_seq
        FROM sw.operations WHERE run_id=:id ORDER BY result_seq NULLS LAST,id LIMIT :limit
    """)
    for operation in operations:
        if operation["result"] is not None:
            operation["result"] = _tool_data(operation["result"])
    report = conn.execute(text("SELECT report FROM sw.runs WHERE id=:id"), params).scalar_one()
    delivery = (
        None
        if report is None
        else {
            **_pick(report, "verification", "baseline_comparable", "final_sha256", "changed_paths"),
            "checks": [
                _pick(check, "operation_id", "tool", "state", "exit_code", "input_sha256")
                for check in report.get("checks", [])
            ],
        }
    )
    return cast(
        JSONDict,
        redact(
            {
                "schema": "stellarweaver.web.run-export.v1",
                "run": _pick(
                    run,
                    "run_id",
                    "session_id",
                    "workspace_id",
                    "status",
                    "prompt",
                    "provider",
                    "mode",
                    "created_at",
                    "outcome",
                    "skills",
                    "context_window_tokens",
                ),
                "messages": messages,
                "operations": operations,
                "events": [_event(row["event"]) for row in events],
                "delivery": delivery,
            }
        ),
    )


def render(bundle: JSONDict, format: str) -> str:
    def serialized(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)

    if format == "json":
        output = serialized(bundle)
    else:
        sections = []
        for key, title in (
            ("run", "运行概览"),
            ("messages", "对话"),
            ("operations", "工具操作"),
            ("events", "按序事件"),
            ("delivery", "验证与交付"),
        ):
            sections.append(
                f"<details{' open' if key == 'run' else ''}><summary>{title}</summary>"
                f"<pre>{escape(serialized(bundle[key]))}</pre></details>"
            )
        output = (
            "<!doctype html><html lang=zh-CN><meta charset=utf-8>"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<title>StellarWeaver 运行记录</title>"
            "<style>body{max-width:70rem;margin:3rem auto;padding:0 1rem;font:16px system-ui}"
            "details{margin:1rem 0}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>"
            "<h1>StellarWeaver 运行记录</h1>" + "".join(sections) + "</html>"
        )
    if len(output.encode()) > MAX_EXPORT_BYTES:
        raise ExportLimitExceeded()
    return output
