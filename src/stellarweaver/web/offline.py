"""Deterministic browser exercise for the small calc.py repair fixture."""

import json

from stellarweaver.domain import AssistantReply, JSONDict, ModelRequest, ToolCall
from stellarweaver.providers.fake import FakeModel
from stellarweaver.web.approval import VERIFY_ARGV

BAD = "return a - b"
WRONG = "return a + b + 1"
FIXED = "return a + b"


def offline_repair_model(
    commands: tuple[tuple[str, ...], ...] = (tuple(VERIFY_ARGV),),
) -> FakeModel:
    argv = list(commands[0])
    phase = 0
    steps: list[tuple[str, JSONDict]] = [
        ("read_file", {"path": "calc.py"}),
        ("edit_file", {"path": "calc.py", "old_text": BAD, "new_text": WRONG}),
        ("run_command", {"argv": argv}),
        ("read_file", {"path": "calc.py"}),
        ("edit_file", {"path": "calc.py", "old_text": WRONG, "new_text": FIXED}),
        ("run_command", {"argv": argv}),
    ]
    steps.extend(("run_command", {"argv": list(command)}) for command in commands[1:])

    def reply(request: ModelRequest) -> AssistantReply:
        nonlocal phase
        latest = next(
            (json.loads(m.content) for m in reversed(request.messages) if m.role == "tool"), None
        )
        if (
            latest
            and latest["status"] != "ok"
            and not (phase == 3 and latest["data"].get("exit_code") == 1)
        ):
            return AssistantReply("离线演示已停止，请查看工具结果。")
        if phase == len(steps):
            return AssistantReply("离线演示已结束，请查看实际测试结果。")
        name, args = steps[phase]
        if name == "edit_file":
            digest = latest["data"].get("sha256") if latest else None
            if latest is None or not digest or args["old_text"] not in latest["content"]:
                return AssistantReply("离线修复演示需要配套的 calc.py 和 test_calc.py。")
            args = {**args, "expected_sha256": digest}
        phase += 1
        return AssistantReply(
            tool_calls=(ToolCall(f"repair-{phase}", name, json.dumps(args)),),
            stop_reason="tool_calls",
        )

    return FakeModel(reply)
