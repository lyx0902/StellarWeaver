"""Evidence-based classification and retry accounting for repair runs."""

from __future__ import annotations

import re
from collections import Counter

from stellarweaver.domain import JSONDict, ToolResult
from stellarweaver.policy.risk import fingerprint

_PATTERNS = (
    (
        "IMPORT_OR_DEPENDENCY",
        re.compile(r"\b(?:ModuleNotFoundError|ImportError)\b|No module named"),
    ),
    (
        "SYNTAX_OR_RUNTIME",
        re.compile(
            r"\b(?:SyntaxError|IndentationError|NameError|TypeError|ValueError|RuntimeError)\b"
        ),
    ),
    ("ASSERTION_FAILED", re.compile(r"\bAssertionError\b|(?m:^FAILED\s+\S+\s*-)|\bassert\s+.+==")),
)
_CATEGORIES = {
    "FILE_CHANGED": "PATCH_CONFLICT",
    "PATCH_BASE_CHANGED": "PATCH_CONFLICT",
    "PATCH_CONFLICT": "PATCH_CONFLICT",
    "PATH_CHANGED": "PATCH_CONFLICT",
    "EDIT_AMBIGUOUS": "PATCH_CONFLICT",
    "EDIT_NOT_FOUND": "PATCH_CONFLICT",
    "PROTECTED_PATH": "SANDBOX_OR_POLICY_DENIED",
    "PATH_OUTSIDE_WORKSPACE": "SANDBOX_OR_POLICY_DENIED",
    "AUTHORIZATION_CHANGED": "SANDBOX_OR_POLICY_DENIED",
    "COMMAND_NOT_CONFIGURED": "SANDBOX_OR_POLICY_DENIED",
    "PERMISSION_DENIED": "SANDBOX_OR_POLICY_DENIED",
    "SANDBOX_UNAVAILABLE": "INFRASTRUCTURE_ERROR",
    "RUNNER_IMAGE_UNAVAILABLE": "INFRASTRUCTURE_ERROR",
    "RUNNER_CREATE_FAILED": "INFRASTRUCTURE_ERROR",
    "PROCESS_CONTAINMENT_FAILED": "INFRASTRUCTURE_ERROR",
    "COMMAND_TIMEOUT": "TIMEOUT",
}
_SUGGESTIONS = {
    "ASSERTION_FAILED": "Inspect the first failing assertion and reproduce it with a focused configured test.",
    "IMPORT_OR_DEPENDENCY": "Check imports and the approved runner recipe before changing application logic.",
    "SYNTAX_OR_RUNTIME": "Inspect the reported frame and verify the edited source in the sandbox.",
    "PATCH_CONFLICT": "Read the current file and use its latest hash and exact matching text.",
    "SANDBOX_OR_POLICY_DENIED": "Use an operation permitted by the current policy or await user direction.",
    "INFRASTRUCTURE_ERROR": "Check executor readiness and the frozen image or dependencies.",
    "TIMEOUT": "Narrow validation to a relevant test within the existing limits.",
    "UNKNOWN": "Inspect the recorded evidence before taking further action.",
    "CANCELLED": "Keep the recorded state for review.",
}
_TEST_FAILURE = re.compile(r"(?m)^(?:FAILED\s+(\S+)|FAIL:\s+(\w+))")


def classify(result: ToolResult) -> str | None:
    if result.status in {"ok", "cancelled", "denied", "blocked", "unknown"}:
        return {
            "ok": None,
            "cancelled": "CANCELLED",
            "denied": "SANDBOX_OR_POLICY_DENIED",
            "blocked": "SANDBOX_OR_POLICY_DENIED",
            "unknown": "UNKNOWN",
        }[result.status]
    data = result.data
    if data.get("timed_out"):
        return "TIMEOUT"
    code = data.get("code")
    if isinstance(code, str) and code in _CATEGORIES:
        return _CATEGORIES[code]
    if data.get("output_incomplete"):
        return "UNKNOWN"
    if type(data.get("exit_code")) is int and data["exit_code"] != 0:
        stdout = str(data.get("stdout", ""))[-12000:]
        stderr = str(data.get("stderr", ""))[-12000:]
        for category, pattern in _PATTERNS:
            if pattern.search(stderr + "\n" + stdout):
                return category
    return "UNKNOWN"


class RepairController:
    def __init__(self, *, enforce: bool) -> None:
        self.enforce = enforce
        self.edits: dict[str, tuple[str | None, str]] = {}
        self.failures: Counter[tuple[str | None, str, str]] = Counter()
        self.notes: JSONDict | None = None
        self._note_subject: str | None = None

    def observe(self, result: ToolResult, *, command: bool) -> tuple[JSONDict | None, bool]:
        data = result.data
        path, after = data.get("path"), data.get("after_sha256")
        if (
            result.tool_name in {"create_file", "edit_file"}
            and result.status == "ok"
            and isinstance(path, str)
            and isinstance(after, str)
        ):
            initial = self.edits.get(path, (data.get("before_sha256"), after))[0]
            if initial == after:
                self.edits.pop(path, None)
            else:
                self.edits[path] = (initial, after)
            self.notes = None
        candidate = fingerprint(self.edits) if self.edits else None
        diagnostic = data.get("diagnostic", {})
        details = data.get("details", {})
        target = (
            (diagnostic.get("target") if isinstance(diagnostic, dict) else None)
            or data.get("argv")
            or path
            or (details.get("path") if isinstance(details, dict) else None)
        )
        subject = fingerprint([result.tool_name, target])
        if (
            command
            and result.tool_name != "discover_tests"
            and result.status == "ok"
            and data.get("exit_code") == 0
        ):
            for key in tuple(self.failures):
                if key[0] == candidate and key[1] == subject:
                    del self.failures[key]
            if self._note_subject == subject:
                self.notes = None
            return {
                "category": "CHECK_PASSED",
                "operation_id": result.operation_id,
                "candidate_edit_sha256": candidate,
                "tool": result.tool_name,
            }, False
        category = classify(result)
        if category is None:
            return None, False
        # Ignore volatile timestamps, host paths and raw log wording in the retry key.
        output = str(data.get("stderr", ""))[-4000:] + "\n" + str(data.get("stdout", ""))[-4000:]
        tests = sorted({match[1] or match[2] for match in _TEST_FAILURE.finditer(output)})[:20]
        signature = fingerprint([subject, category, data.get("code"), data.get("exit_code"), tests])
        key = (candidate, subject, signature)
        self.failures[key] += 1
        count = self.failures[key]
        stalled = (
            count >= 3 and result.status == "error" and (command or category == "PATCH_CONFLICT")
        )
        event = {
            "category": "NO_PROGRESS" if stalled else category,
            "failure_category": category,
            "operation_id": result.operation_id,
            "tool": result.tool_name,
            "signature_sha256": signature,
            "candidate_edit_sha256": candidate,
            "repeat_count": count,
            "exit_code": data.get("exit_code"),
            "next_step": _SUGGESTIONS[category],
        }
        self.notes = event
        self._note_subject = subject
        return event, stalled and self.enforce
