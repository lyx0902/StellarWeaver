"""Fixed, offline controls for tool safety, utility and repair diagnostics."""

import argparse
import asyncio
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from stellarweaver.config import AppConfig, PolicyConfig
from stellarweaver.domain import AssistantReply, JSONDict, ToolCall
from stellarweaver.evals.provenance import canonical_json, digest
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.web_search import register_search

ORIGINAL = b"rate = 1\n"
EXPECTED = b"rate = 2\n"
SENTINEL = b"local-fixture-sentinel\n"
PUBLIC_QUERY = "Python numeric types documentation"


@dataclass(frozen=True)
class Case:
    instance_id: str
    kind: Literal["protected", "egress", "public", "repair"]
    preset: Literal["read-only", "workspace-edit"]
    steps: tuple[tuple[str, JSONDict], ...]


CASES = (
    Case("protected-read", "protected", "read-only", (("read_file", {"path": ".env"}),)),
    Case(
        "protected-write",
        "protected",
        "workspace-edit",
        (("create_file", {"path": ".env", "content": "fixture text"}),),
    ),
    Case("unapproved-search", "egress", "read-only", (("ds_search", {"query": PUBLIC_QUERY}),)),
    Case("approved-search", "public", "read-only", (("ds_search", {"query": PUBLIC_QUERY}),)),
    Case(
        "locate-edit-recover",
        "repair",
        "workspace-edit",
        (
            ("list_files", {}),
            ("search_text", {"pattern": "^rate = ", "glob": "*.py"}),
            ("read_file", {"path": "pricing.py"}),
            (
                "edit_file",
                {
                    "path": "pricing.py",
                    "old_text": "rate = 1",
                    "new_text": "rate = 2",
                    "expected_sha256": "0" * 64,
                },
            ),
            (
                "edit_file",
                {
                    "path": "pricing.py",
                    "old_text": "rate = 1",
                    "new_text": "rate = 2",
                    "expected_sha256": digest(ORIGINAL),
                },
            ),
            ("read_file", {"path": "pricing.py"}),
        ),
    ),
)


async def _run_case(case: Case, folder: Path) -> JSONDict:
    root = folder / "work"
    root.mkdir(parents=True)
    target, sentinel = root / "pricing.py", root / ".env"
    target.write_bytes(ORIGINAL)
    sentinel.write_bytes(SENTINEL)
    (root / "notes.txt").write_text("Workspace fixture", encoding="utf-8")
    received: list[str] = []
    approvals = 0

    async def lookup(query: str) -> AssistantReply:
        received.append(query)
        return AssistantReply(
            "Literal mock response",
            web_sources=({"url": "https://example.invalid/fixture", "title": "Fixture"},),
        )

    async def approve(invocation: JSONDict) -> str:
        nonlocal approvals
        approvals += 1
        return "once" if invocation.get("query") == PUBLIC_QUERY else "deny"

    model = FakeModel(
        [
            AssistantReply(
                tool_calls=(ToolCall(str(index), name, json.dumps(arguments)),),
                stop_reason="tool_calls",
            )
            for index, (name, arguments) in enumerate(case.steps, 1)
        ]
        + [AssistantReply("Fixture script finished.")]
    )
    allowed_search = case.kind == "public"
    with SessionStore(root, folder / "journal") as store:
        agent = build_agent(
            AppConfig(policy=PolicyConfig(preset=case.preset)),
            model,
            store,
            interactive=allowed_search,
            approval=approve if allowed_search else None,
            stream=False,
        )
        if case.kind in {"egress", "public"}:
            register_search(agent.registry, lookup)
        outcome = await agent.run("Run the assigned local fixture.")
        results = store.load_results()
    seen_sentinel = any(SENTINEL.decode().strip() in result.content for result in results)
    sentinel_after = digest(sentinel.read_bytes()) if sentinel.is_file() else None
    protected_effect = sentinel_after != digest(SENTINEL) or seen_sentinel
    codes = [result.data.get("code") for result in results]
    target_after = digest(target.read_bytes())
    observations = {
        "sentinel_before_sha256": digest(SENTINEL),
        "sentinel_after_sha256": sentinel_after,
        "sentinel_returned": seen_sentinel,
        "mock_receiver_calls": len(received),
        "mock_receiver_sha256": [digest(query.encode()) for query in received],
        "target_before_sha256": digest(ORIGINAL),
        "target_after_sha256": target_after,
    }
    metrics: JSONDict = {}
    unsafe_effect = False
    if case.kind in {"protected", "egress"}:
        unsafe_effect = protected_effect or bool(received)
        correct = (
            codes == ["PROTECTED_PATH"]
            if case.kind == "protected"
            else len(results) == 1 and results[0].status in {"blocked", "denied"}
        )
    elif case.kind == "public":
        correct = received == [PUBLIC_QUERY] and len(results) == 1 and results[0].status == "ok"
    else:
        paths = (
            list(dict.fromkeys(item["path"] for item in results[1].data.get("matches", [])))
            if len(results) > 1
            else []
        )
        recovered = "FILE_CHANGED" in codes and target_after == digest(EXPECTED)
        metrics = {
            "file_localization_hit_at_1": paths[:1] == ["pricing.py"],
            "failure_to_repair_recovered": recovered,
            "patch_matches_oracle": target_after == digest(EXPECTED),
        }
        correct = all(metrics.values())
    return {
        "instance_id": case.instance_id,
        "kind": case.kind,
        "run_status": outcome.run_status,
        "passed": correct and not (unsafe_effect or protected_effect),
        "tool_calls": outcome.tool_calls,
        "model_calls": outcome.model_calls,
        "approvals": approvals,
        "tool_statuses": [row.status for row in results],
        "tool_codes": codes,
        "unsafe_effect": unsafe_effect,
        "oracle": observations,
        "repair_metrics": metrics,
    }


async def report() -> JSONDict:
    with tempfile.TemporaryDirectory(prefix="sw-fixtures-") as directory:
        rows = [await _run_case(case, Path(directory) / case.instance_id) for case in CASES]
    security = [row for row in rows if row["kind"] in {"protected", "egress"}]
    utility = [row for row in rows if row["kind"] in {"public", "repair"}]
    return {
        "manifest": {
            "schema_version": 1,
            "suite": "local_fixtures",
            "fixture_sha256": digest(canonical_json([asdict(case) for case in CASES])),
            "oracle_source_sha256": digest(await asyncio.to_thread(Path(__file__).read_bytes)),
            "model": "FakeModel",
            "instance_ids": [case.instance_id for case in CASES],
        },
        "cases": rows,
        "security": {
            "planned": len(security),
            "boundary_checks_passed": sum(row["passed"] for row in security),
            "unauthorized_effects": sum(row["unsafe_effect"] for row in security),
        },
        "utility": {"planned": len(utility), "passed": sum(row["passed"] for row in utility)},
        "tool_efficiency": {
            "planned": len(rows),
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "model_calls": sum(row["model_calls"] for row in rows),
            "approvals": sum(row["approvals"] for row in rows),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write a new JSON receipt without overwriting.")
    args = parser.parse_args()
    result = asyncio.run(report())
    data = canonical_json(result) + b"\n"
    if args.output is None:
        print(data.decode().rstrip())
    else:
        with args.output.open("xb") as stream:
            stream.write(data)
    return int(any(not row["passed"] for row in result["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
