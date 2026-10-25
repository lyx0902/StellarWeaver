"""Pure, source-labelled rendering of immutable session snapshots.

These functions neither open a provider nor interpret journal entries as actions.
JSONL is a static export of the SQLite authority, never another writable store.
"""

import json
from collections.abc import Iterator
from typing import Any, Protocol

from stellarweaver.domain import JSONDict


class SnapshotView(Protocol):
    def as_dict(self) -> JSONDict: ...


def _data(snapshot: SnapshotView) -> JSONDict:
    data = snapshot.as_dict()
    return {**data["metadata"], **data}


def records(snapshot: SnapshotView) -> Iterator[JSONDict]:
    data = _data(snapshot)
    source = {
        "session_id": data["session_id"],
        "path": str(data["path"]),
        "schema_version": data["schema_version"],
        "mode": "historical",
    }
    yield {
        "record_type": "session",
        "source": source,
        "payload": {key: data[key] for key in ("session_id", "workspace", "status", "metadata")},
    }
    for collection, kind in (
        ("runs", "run"),
        ("messages", "message"),
        ("tool_executions", "tool_execution"),
        ("tool_results", "tool_result"),
        ("events", "event"),
        ("checkpoints", "checkpoint"),
    ):
        for position, item in enumerate(data.get(collection, ())):
            yield {"record_type": kind, "source": source, "position": position, "payload": item}


def render_jsonl(snapshot: SnapshotView) -> str:
    # ASCII-safe JSON preserves arbitrary Unicode even on legacy Windows stdout.
    return "".join(json.dumps(record, ensure_ascii=True) + "\n" for record in records(snapshot))


def _block(value: Any) -> str:
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    # Content, including arbitrary tool output, cannot terminate the surrounding fence.
    fence = "`" * max(
        3, max((len(part) for part in content.split("\n") if set(part) == {"`"}), default=0) + 1
    )
    while fence in content:
        fence += "`"
    return f"{fence}\n{content}\n{fence}\n"


def render_markdown(snapshot: SnapshotView) -> str:
    data = _data(snapshot)
    output = [
        "# StellarWeaver historical session\n",
        f"Session: `{data['session_id']}`  \n",
        f"Workspace: `{data['workspace']}`  \n",
        f"Source SQLite: `{data['path']}`  \n",
        f"Schema: {data['schema_version']}; recorded status: {data['status']}\n\n",
        "This is a static historical record. No model request or tool action is executed.\n\n",
    ]
    for record in records(snapshot):
        kind = record["record_type"]
        payload = record["payload"]
        if kind == "session":
            continue
        label = {
            "message": f"Message: {payload.get('role', 'unknown')}",
            "event": f"Event: {payload.get('type', 'unknown')}",
            "tool_execution": "Tool operation evidence",
            "tool_result": "Tool result evidence",
            "run": "Recorded run outcome",
            "checkpoint": "Context checkpoint",
        }[kind]
        output.append(f"## {label}\n\n")
        output.append(_block(payload) + "\n")
    return "".join(output)
