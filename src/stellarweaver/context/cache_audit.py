"""Read-only sidecar auditor that detects unexpected Prefix Cache breaks and byte offsets."""

from __future__ import annotations

import json
from dataclasses import dataclass

from stellarweaver.domain import JSONDict, Message, ModelRequest


def _canonical_message(message: Message) -> str:
    payload: JSONDict = {
        "role": message.role,
        "content": message.content,
    }
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "call_id": call.call_id,
                "name": call.name,
                "arguments_json": call.arguments_json,
            }
            for call in message.tool_calls
        ]
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _first_diff_detail(prev: str, curr: str, *, radius: int = 80) -> tuple[int, str, str]:
    limit = min(len(prev), len(curr))
    offset = limit
    for idx in range(limit):
        if prev[idx] != curr[idx]:
            offset = idx
            break
    start = max(0, offset - radius)
    return offset, prev[start : offset + radius], curr[start : offset + radius]


@dataclass
class _PrefixSnapshot:
    model_key: str
    tools_json: str
    messages: tuple[str, ...]
    roles: tuple[str, ...]


class PrefixCacheAuditor:
    """Zero-mutation observer tracking append-only request prefix stability across turns."""

    def __init__(self) -> None:
        self._last: _PrefixSnapshot | None = None

    def reset(self) -> None:
        self._last = None

    def audit(
        self,
        request: ModelRequest,
        *,
        model_key: str,
        compacted: bool,
        request_index: int,
    ) -> JSONDict | None:
        tools_json = json.dumps(request.tools, sort_keys=True, ensure_ascii=False)
        msgs = tuple(_canonical_message(message) for message in request.messages)
        roles = tuple(message.role for message in request.messages)
        curr = _PrefixSnapshot(
            model_key=model_key,
            tools_json=tools_json,
            messages=msgs,
            roles=roles,
        )
        prev = self._last
        self._last = curr
        if prev is None or compacted:
            return None

        checks = (
            ("model", prev.model_key, curr.model_key),
            ("tools", prev.tools_json, curr.tools_json),
        )
        for section, prev_text, curr_text in checks:
            if prev_text != curr_text:
                offset, prev_sample, curr_sample = _first_diff_detail(prev_text, curr_text)
                return {
                    "request_index": request_index,
                    "first_diff_section": section,
                    "first_diff_offset": offset,
                    "prev_sample": prev_sample,
                    "curr_sample": curr_sample,
                    "prev_message_count": len(prev.messages),
                    "curr_message_count": len(curr.messages),
                }

        if len(curr.messages) < len(prev.messages):
            prev_tail = prev.messages[len(curr.messages)]
            return {
                "request_index": request_index,
                "first_diff_section": "messages_shrunk",
                "first_diff_offset": 0,
                "prev_sample": prev_tail[:160],
                "curr_sample": "",
                "prev_message_count": len(prev.messages),
                "curr_message_count": len(curr.messages),
            }

        for idx, prev_msg in enumerate(prev.messages):
            curr_msg = curr.messages[idx]
            if prev_msg != curr_msg:
                offset, prev_sample, curr_sample = _first_diff_detail(prev_msg, curr_msg)
                section = (
                    "system"
                    if prev.roles[idx] == "system" or curr.roles[idx] == "system"
                    else f"message[{idx}]"
                )
                return {
                    "request_index": request_index,
                    "first_diff_section": section,
                    "first_diff_offset": offset,
                    "prev_sample": prev_sample,
                    "curr_sample": curr_sample,
                    "prev_message_count": len(prev.messages),
                    "curr_message_count": len(curr.messages),
                }

        return None
