"""Read-only sidecar auditor that detects unexpected Prefix Cache breaks and summary offsets."""

from __future__ import annotations

import hashlib
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


def _short_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _first_diff_offset_chars(prev: str, curr: str) -> int:
    limit = min(len(prev), len(curr))
    for idx in range(limit):
        if prev[idx] != curr[idx]:
            return idx
    return limit


def _build_divergence_event(
    *,
    request_index: int,
    common_prefix_messages: int,
    section: str,
    offset_chars: int,
    prev_text: str,
    curr_text: str,
    prev_message_count: int,
    curr_message_count: int,
) -> JSONDict:
    return {
        "request_index": request_index,
        "common_prefix_messages": common_prefix_messages,
        "first_diff_section": section,
        "first_diff_offset_chars": offset_chars,
        "offset_unit": "chars",
        "prev_sha256": _short_sha256(prev_text) if prev_text else "",
        "curr_sha256": _short_sha256(curr_text) if curr_text else "",
        "prev_chars": len(prev_text),
        "curr_chars": len(curr_text),
        "prev_message_count": prev_message_count,
        "curr_message_count": curr_message_count,
        "messages_shrunk": curr_message_count < prev_message_count,
    }


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
                return _build_divergence_event(
                    request_index=request_index,
                    common_prefix_messages=0,
                    section=section,
                    offset_chars=_first_diff_offset_chars(prev_text, curr_text),
                    prev_text=prev_text,
                    curr_text=curr_text,
                    prev_message_count=len(prev.messages),
                    curr_message_count=len(curr.messages),
                )

        # Always compare overlapping prefix positions element-by-element first, even
        # if curr.messages is shorter, so an earlier mutation is attributed accurately.
        shared_len = min(len(prev.messages), len(curr.messages))
        for idx in range(shared_len):
            prev_msg = prev.messages[idx]
            curr_msg = curr.messages[idx]
            if prev_msg != curr_msg:
                section = (
                    "system"
                    if prev.roles[idx] == "system" or curr.roles[idx] == "system"
                    else f"message[{idx}]"
                )
                return _build_divergence_event(
                    request_index=request_index,
                    common_prefix_messages=idx,
                    section=section,
                    offset_chars=_first_diff_offset_chars(prev_msg, curr_msg),
                    prev_text=prev_msg,
                    curr_text=curr_msg,
                    prev_message_count=len(prev.messages),
                    curr_message_count=len(curr.messages),
                )

        if len(curr.messages) < len(prev.messages):
            return _build_divergence_event(
                request_index=request_index,
                common_prefix_messages=len(curr.messages),
                section="messages_shrunk",
                offset_chars=0,
                prev_text=prev.messages[len(curr.messages)],
                curr_text="",
                prev_message_count=len(prev.messages),
                curr_message_count=len(curr.messages),
            )

        return None
