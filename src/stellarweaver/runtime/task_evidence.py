"""Bounded evidence and budget guidance derived from persisted tool results."""

from __future__ import annotations

import re
from collections import deque

from stellarweaver.domain import JSONDict, ToolResult
from stellarweaver.policy.risk import fingerprint
from stellarweaver.runtime.repair import classify
from stellarweaver.tools.workspace import ToolError, Workspace, sha256_bytes

_SHA = re.compile(r"[a-f0-9]{64}")
_VERIFICATION = frozenset({"run_tests", "run_visible_tests", "run_command", "run_reproduction"})


class TaskEvidence:
    def __init__(self) -> None:
        self.paths: dict[str, JSONDict] = {}
        self.edits: dict[str, str] = {}
        self.observations: deque[JSONDict] = deque(maxlen=8)
        self.staged: JSONDict | None = None
        self.last_check: str | None = None

    def observe(self, result: ToolResult, seq: int) -> None:
        data = result.data
        anchor = {"operation_id": result.operation_id, "event_seq": seq}
        if result.status == "ok":
            facts = (
                (data.get("matches", []) or [])[:8]
                if result.tool_name in {"find_symbol", "find_references", "find_related_tests"}
                else [data]
                if result.tool_name in {"read_file", "edit_file", "create_file"}
                else []
            )
            for fact in facts:
                path, checksum = fact.get("path"), fact.get("after_sha256", fact.get("sha256"))
                if (
                    isinstance(path, str)
                    and len(path) <= 240
                    and path.isprintable()
                    and isinstance(checksum, str)
                    and _SHA.fullmatch(checksum)
                ):
                    self.paths.pop(path, None)
                    self.paths[path] = {"path": path, "sha256": checksum, **anchor}
                    if len(self.paths) > 12:
                        del self.paths[next(iter(self.paths))]
                    if (
                        result.tool_name in {"edit_file", "create_file"}
                        and data.get("before_sha256") != checksum
                    ):
                        self.edits[path] = checksum
                        self.last_check = None
                        self.staged = None
            if result.tool_name == "stage_patch":
                self.staged = {"patch_sha256": data["patch_sha256"], **anchor}
        if result.tool_name in _VERIFICATION:
            self.last_check = result.status
        category = classify(result)
        if category is not None or result.tool_name in _VERIFICATION:
            self.observations.append(
                {
                    **anchor,
                    "tool": result.tool_name,
                    "status": result.status,
                    "category": category,
                    "exit_code": data.get("exit_code")
                    if type(data.get("exit_code")) is int
                    else None,
                    "candidate_edit_sha256": fingerprint(self.edits) if self.edits else None,
                }
            )

    def project(self, workspace: Workspace, goal: str) -> JSONDict | None:
        if not (self.paths or self.observations or self.staged):
            return None
        fresh = []
        stale = 0
        for item in self.paths.values():
            try:
                target = workspace.resolve(item["path"])
                with target.open("rb") as file:
                    raw = file.read(1024 * 1024 + 1)
                if len(raw) <= 1024 * 1024 and sha256_bytes(raw) == item["sha256"]:
                    fresh.append(item)
                    continue
            except (ToolError, OSError):
                pass
            stale += 1
        return {
            "goal_sha256": sha256_bytes(goal.encode()),
            "files": fresh[-5:],
            "stale_paths": stale,
            "observations": list(self.observations)[-4:],
            "staged_patch": self.staged,
        }

    def guidance(
        self,
        model_left: int | None,
        tools_left: int,
        wall_left: float,
        input_left: int,
        output_left: int,
    ) -> JSONDict:
        constrained = (
            (model_left is not None and model_left <= 2)
            or tools_left <= 3
            or wall_left <= 30
            or input_left <= 6000
            or output_left <= 512
        )
        if self.edits:
            phase = "deliver" if self.last_check == "ok" else "verify"
        elif self.staged:
            phase = "review"
        elif self.paths:
            phase = "edit" if self.last_check == "error" else "reproduce"
        else:
            phase = "locate"
        return {
            "phase": phase,
            "capacity_limited": constrained,
            "model_calls_remaining": model_left,
            "tool_calls_remaining": max(0, tools_left),
            "wall_seconds_remaining": max(0, int(wall_left)),
            "input_tokens_remaining": max(0, input_left),
            "output_tokens_remaining": max(0, output_left),
            "recommendation": (
                "Prioritize one relevant authorized check for a changed candidate, or report verification as incomplete."
                if constrained
                else "Use the current phase and preserve capacity for verification."
            ),
        }
