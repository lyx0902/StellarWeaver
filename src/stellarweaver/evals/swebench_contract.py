"""Normalize saved SWE-bench evidence without running a provider or grader."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from stellarweaver.domain import JSONDict
from stellarweaver.evals.provenance import canonical_json, digest
from stellarweaver.evals.swebench import (
    ID_PATTERN,
    IMAGE_PATTERN,
    SHA_PATTERN,
    Prediction,
    Recipe,
    Task,
)

_ATTEMPT = re.compile(r"(?:agent|import)-[a-zA-Z0-9_-]+")
_SCORE = re.compile(r"grade-[a-zA-Z0-9_-]+")
_VARIATIONS = ("source_sha256", "config_sha256", "prompt_sha256", "tools_schema_sha256")
Digest = Annotated[str, Field(pattern=SHA_PATTERN)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Evidence(Record):
    path: str
    sha256: Digest
    size: Annotated[int, Field(ge=0)]


class Manifest(Record):
    schema_version: Literal[1] = 1
    experiment_id: str
    suite: Literal["swebench_lite"] = "swebench_lite"
    suite_revision: str | None
    revision_verified: bool
    dataset_sha256: Digest
    split: Literal["dev", "test"]
    instance_ids: list[str]
    harness_commit: str | None
    source_sha256: dict[str, Digest]
    grader_version: str
    limits: JSONDict
    frozen_config_sha256: Digest
    hints: bool
    web_search: bool
    completion_gate: bool
    evidence: list[Evidence]


class Attempt(Record):
    attempt_id: str
    run_status: str
    candidate_status: str
    patch_sha256: Digest | None = None
    reason: str | None = None
    failure_category: str | None = None
    provider: str | None = None
    model_id: str | None = None
    resolved_revision: str | None = None
    thinking: str | None = None
    reasoning_effort: str | None = None
    prompt_sha256: Digest | None = None
    config_sha256: Digest | None = None
    policy_sha256: Digest | None = None
    tools_schema_sha256: Digest | None = None
    fixture_sha256: Digest | None = None
    sandbox_profile: str | None = None
    agent_image: str | None = None
    model_calls: int | None = None
    tool_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    duration_ms: int | None = None
    usage_source: Literal["reported", "estimated", "incomplete", "unavailable"]
    evidence: list[Evidence]


class Score(Record):
    evaluation_run_id: str
    attempt_id: str | None
    evaluation_status: Literal["resolved", "unresolved", "evaluation_error"]
    resolved: bool | None
    patch_sha256: Digest
    grader_version: str
    grader_image: str | None
    scorer_sha256: Digest
    patch_applied: bool | None = None
    reason: str | None = None
    evidence: list[Evidence]


class TaskRecord(Record):
    instance_id: str
    git_commit: str
    latest_attempt_id: str | None
    latest_score_id: str | None
    security_assessment: Literal["not_evaluated"] = "not_evaluated"
    attempts: list[Attempt]
    scores: list[Score]
    evidence: list[Evidence]

    def attempt(self) -> Attempt | None:
        return next(
            (row for row in self.attempts if row.attempt_id == self.latest_attempt_id), None
        )

    def score(self) -> Score | None:
        return next(
            (row for row in self.scores if row.evaluation_run_id == self.latest_score_id), None
        )


class Report(Record):
    manifest: Manifest
    tasks: list[TaskRecord]
    aggregate: JSONDict


class Source:
    """Read only named files below the experiment and keep their byte fingerprints."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.files: dict[Path, Evidence] = {}
        self.absent: set[Path] = set()
        self.listings: dict[tuple[Path, re.Pattern[str]], list[Path]] = {}

    def path(self, path: Path) -> Path:
        if path.resolve() != path or not path.is_relative_to(self.root):
            raise ValueError(f"Evidence path escapes its experiment: {path.name}")
        return path

    def read(self, path: Path) -> bytes:
        value = self.path(path).read_bytes()
        checksum = Evidence(
            path=path.relative_to(self.root).as_posix(), sha256=digest(value), size=len(value)
        )
        if self.files.setdefault(path, checksum) != checksum:
            raise ValueError("Evidence changed during reporting")
        return value

    def json(self, path: Path) -> JSONDict:
        value = json.loads(self.read(path))
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object in {path.name}")
        return value

    def evidence(self, path: Path) -> Evidence:
        if path not in self.files:
            self.read(path)
        return self.files[path]

    def optional(self, path: Path) -> JSONDict:
        if self.path(path).exists():
            return self.json(path)
        self.absent.add(path)
        return {}

    def children(self, directory: Path, pattern: re.Pattern[str]) -> list[Path]:
        found = sorted(
            self.path(path) for path in directory.iterdir() if pattern.fullmatch(path.name)
        )
        if any(not path.is_dir() for path in found):
            raise ValueError("Expected an attempt or score directory")
        if self.listings.setdefault((directory, pattern), found) != found:
            raise ValueError("Experiment directories changed during reporting")
        return found

    def verify(self) -> None:
        for path in tuple(self.files):
            self.read(path)
        if any(self.path(path).exists() for path in self.absent):
            raise ValueError("Experiment files appeared during reporting")
        for directory, pattern in tuple(self.listings):
            self.children(directory, pattern)


def _usage(outcome: JSONDict) -> Literal["reported", "estimated", "incomplete", "unavailable"]:
    if outcome.get("usage_incomplete"):
        return "incomplete"
    if outcome.get("usage_estimated"):
        return "estimated"
    if outcome.get("model_calls", 0) > 0:
        return "reported"
    return "unavailable"


def _attempt(
    src: Source, directory: Path, pointer: JSONDict, fixture: str | None, instance_id: str
) -> Attempt:
    report_path = directory / "agent-report.json"
    receipt_path = directory / "attempt.json"
    report = src.optional(report_path)
    receipt = src.optional(receipt_path)
    if receipt and receipt.get("attempt") != directory.name:
        raise ValueError("Attempt receipt identity mismatch")
    if receipt and pointer and pointer.get("status") != "running" and receipt != pointer:
        raise ValueError("Current attempt pointer disagrees with its receipt")
    current = receipt or pointer
    outcome = report.get("outcome", {})
    run_status = outcome.get("run_status") or current.get("status", "unknown")
    if run_status == "running":
        run_status = "interrupted"
    candidate = report.get("candidate_status") or current.get("candidate_status")
    sha = report.get("patch_sha256") or current.get("patch_sha256")
    patch = directory / "candidate.patch"
    prediction = directory / "prediction.json"
    files = [path for path in (receipt_path, report_path) if path in src.files]
    for path in (patch, prediction):
        if not src.path(path).exists():
            continue
        value = src.read(path)
        files.append(path)
        if sha is None:
            continue  # An interrupted attempt can have bytes before any candidate was sealed.
        if path == prediction:
            decoded = Prediction.model_validate_json(value)
            if decoded.instance_id != instance_id:
                raise ValueError("Prediction belongs to a different instance")
            value = decoded.model_patch.encode("utf-8")
        if digest(value) != sha:
            raise ValueError("Candidate patch hash mismatch")
    if candidate in {"valid_patch", "provided_patch"} and prediction not in files:
        raise ValueError("Candidate prediction is missing")
    # The Journal stays in the source directory. The portable record contains only its locator/hash.
    session = report.get("session_id")
    if isinstance(session, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", session):
        journal = directory / "state" / session / "session.sqlite3"
        if src.path(journal).is_file():
            files.append(journal)
    return Attempt(
        attempt_id=directory.name,
        run_status=run_status,
        candidate_status=candidate or ("unknown" if run_status == "unknown" else "not_produced"),
        patch_sha256=sha,
        reason=outcome.get("reason") or current.get("reason"),
        failure_category=report.get("failure_category")
        or ("environment_error" if run_status == "environment_error" else None),
        provider=report.get("provider"),
        model_id=report.get("model"),
        resolved_revision=report.get("resolved_revision"),
        thinking=report.get("thinking"),
        reasoning_effort=report.get("reasoning_effort"),
        prompt_sha256=report.get("prompt_sha256"),
        config_sha256=report.get("config_sha256"),
        policy_sha256=report.get("policy_sha256"),
        tools_schema_sha256=report.get("tools_sha256"),
        fixture_sha256=fixture,
        sandbox_profile=report.get("sandbox_profile"),
        agent_image=report.get("agent_image"),
        model_calls=outcome.get("model_calls"),
        tool_calls=outcome.get("tool_calls"),
        input_tokens=outcome.get("input_tokens"),
        output_tokens=outcome.get("output_tokens"),
        cached_input_tokens=report.get("cached_input_tokens", outcome.get("cached_input_tokens")),
        duration_ms=outcome.get("duration_ms"),
        usage_source=_usage(outcome),
        evidence=[src.evidence(path) for path in files],
    )


def _score(
    src: Source,
    case: Path,
    run_id: str,
    raw: JSONDict,
    attempts: list[Attempt],
    expected: JSONDict,
    paths: list[Path],
) -> Score:
    if raw.get("run_id") != run_id or raw.get("instance_id", case.name) != case.name:
        raise ValueError("Score receipt identity mismatch")
    matches = [
        item.attempt_id
        for item in attempts
        if item.patch_sha256 is not None and item.patch_sha256 == raw.get("patch_sha256")
    ]
    explicit = raw.get("attempt_id")
    if not matches or (explicit is not None and explicit not in matches):
        raise ValueError("Score is not bound to a matching candidate")
    attempt_id = explicit if explicit is not None else (matches[0] if len(matches) == 1 else None)
    status = raw.get("status")
    if status not in {"resolved", "unresolved", "evaluation_error", "evaluating"}:
        raise ValueError("Unexpected score status")
    graded = status in {"resolved", "unresolved"}
    resolved = raw.get("resolved")
    if resolved is not ((status == "resolved") if graded else None):
        raise ValueError("Score verdict contradicts its status")
    if any(raw.get(key) != value for key, value in expected.items()):
        raise ValueError("Score does not match the frozen scorer, version or image")
    raw_report = case / run_id / "report.json"
    if graded:
        image = raw.get("image")
        if not isinstance(image, str) or not re.fullmatch(IMAGE_PATTERN, image):
            raise ValueError("Official score requires a pinned grader image")
        if raw.get("raw_report") != f"{run_id}/report.json":
            raise ValueError("Official score does not name its raw report")
        official = src.json(raw_report).get(case.name)
        if (
            raw.get("raw_report_sha256", src.evidence(raw_report).sha256)
            != src.evidence(raw_report).sha256
        ):
            raise ValueError("Official report hash mismatch")
        if (
            not isinstance(official, dict)
            or official.get("infra_failure")
            or official.get("resolved") is not resolved
        ):
            raise ValueError("Official report contradicts the receipt")
        paths.append(raw_report)
    grader_log = case / run_id / "grader.log"
    if src.path(grader_log).is_file():
        paths.append(grader_log)
    return Score(
        evaluation_run_id=run_id,
        attempt_id=attempt_id,
        evaluation_status="evaluation_error" if status == "evaluating" else status,
        resolved=resolved,
        patch_sha256=raw["patch_sha256"],
        grader_version=raw["grader_version"],
        grader_image=raw.get("image"),
        scorer_sha256=raw["scorer_sha256"],
        patch_applied=raw.get("patch_applied"),
        reason="evaluation_interrupted" if status == "evaluating" else raw.get("reason"),
        evidence=[src.evidence(path) for path in paths],
    )


def _task(src: Source, key: str, manifest: JSONDict, fixture: str | None) -> TaskRecord:
    case = src.path(src.root / "cases" / key)
    task_path = case / "task.json"
    task = Task.model_validate_json(src.read(task_path))
    if task.instance_id != key:
        raise ValueError("Public task identity mismatch")
    scorer_path = case / "scorer.json"
    scorer = src.json(scorer_path)
    if scorer.get("instance_id") != key or scorer.get("base_commit") != task.base_commit:
        raise ValueError("Scorer does not match the public task identity")
    recipe_path = case / "recipe.json"
    inputs = [task_path, scorer_path]
    recipe = None
    if src.path(recipe_path).exists():
        recipe = Recipe.model_validate_json(src.read(recipe_path))
        inputs.append(recipe_path)
    if {path.name: src.evidence(path).sha256 for path in inputs} != manifest["inputs"][key]:
        raise ValueError(f"Frozen input hash mismatch for {key}")

    current_attempt = src.optional(case / "solve.json")
    active = current_attempt.get("attempt")
    attempts = [
        _attempt(src, entry, current_attempt if entry.name == active else {}, fixture, key)
        for entry in src.children(case, _ATTEMPT)
    ]
    if active is not None and active not in {item.attempt_id for item in attempts}:
        raise ValueError("Current attempt pointer has no receipt directory")

    pointer_path = case / "evaluation.json"
    current_score = src.optional(pointer_path)
    score_id = current_score.get("run_id")
    expected = {
        "scorer_sha256": src.evidence(scorer_path).sha256,
        "grader_version": manifest["official_grader_version"],
        "image": (recipe.grader_image if recipe else None) or scorer.get("image"),
    }
    scores = []
    for entry in src.children(case, _SCORE):
        receipt_path = entry / "evaluation.json"
        receipt = src.optional(receipt_path)
        if entry.name == score_id:
            if receipt and receipt != current_score and current_score.get("status") != "evaluating":
                raise ValueError("Current score pointer disagrees with its receipt")
            scores.append(
                _score(
                    src,
                    case,
                    entry.name,
                    receipt or current_score,
                    attempts,
                    expected,
                    [pointer_path, receipt_path] if receipt else [pointer_path],
                )
            )
        elif receipt:
            scores.append(
                _score(src, case, entry.name, receipt, attempts, expected, [receipt_path])
            )
    if score_id is not None and score_id not in {score.evaluation_run_id for score in scores}:
        raise ValueError("Current score pointer has no receipt directory")
    return TaskRecord(
        instance_id=key,
        git_commit=task.base_commit,
        latest_attempt_id=active,
        latest_score_id=score_id,
        attempts=attempts,
        scores=scores,
        evidence=[
            src.evidence(path) for path in [*inputs, case / "solve.json"] if path in src.files
        ],
    )


def aggregate_tasks(tasks: list[TaskRecord]) -> JSONDict:
    runs: Counter[str] = Counter()
    candidates: Counter[str] = Counter()
    grades: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    for task in tasks:
        attempt, score = task.attempt(), task.score()
        runs[attempt.run_status if attempt else "not_started"] += 1
        candidates[attempt.candidate_status if attempt else "not_started"] += 1
        if attempt and attempt.failure_category:
            failures[attempt.failure_category] += 1
        grades[
            score.evaluation_status
            if score and attempt and score.attempt_id == attempt.attempt_id
            else "not_evaluated"
        ] += 1
    return {
        "planned": len(tasks),
        "by_run_status": dict(runs),
        "by_candidate_status": dict(candidates),
        "by_evaluation_status": dict(grades),
        "by_failure_category": dict(failures),
        "not_started": runs["not_started"],
        "no_patch": candidates["empty_patch"],
        "evaluation_error": grades["evaluation_error"],
        "graded": grades["resolved"] + grades["unresolved"],
        "resolved_rate": grades["resolved"] / len(tasks),
    }


def read_report(root: Path) -> Report:
    src = Source(root)
    path = src.root / "manifest.json"
    raw = src.json(path)
    ids = raw["instance_ids"]
    if (
        raw.get("schema_version") != 1
        or not isinstance(ids, list)
        or not ids
        or len(ids) != len(set(ids))
    ):
        raise ValueError("Expected a nonempty v1 SWE-bench plan with unique instances")
    if not isinstance(raw.get("dataset_sha256"), str) or not re.fullmatch(
        SHA_PATTERN, raw["dataset_sha256"]
    ):
        raise ValueError("Missing frozen dataset checksum")
    for key in ids:
        if not isinstance(key, str) or not re.fullmatch(ID_PATTERN, key):
            raise ValueError("Invalid planned instance ID")
    if set(raw["inputs"]) != set(ids):
        raise ValueError("Frozen inputs do not match the planned instances")
    settings_path = src.root / "run-settings.json"
    settings = src.optional(settings_path)
    if settings and settings["limits"] != raw["limits"]:
        raise ValueError("Recorded running limits differ from the frozen plan")
    manifest_hash = src.evidence(path).sha256
    manifest = Manifest(
        experiment_id=f"swebench-lite-{manifest_hash}",
        suite_revision=raw.get("dataset_revision"),
        revision_verified=raw["dataset_revision_verified"],
        dataset_sha256=raw["dataset_sha256"],
        split=raw["split"],
        instance_ids=ids,
        harness_commit=settings.get("code_sha", raw.get("code_sha")),
        source_sha256=TypeAdapter(dict[str, Digest]).validate_python(
            settings.get("source_sha256", raw["source_sha256"]), strict=True
        ),
        grader_version=raw["official_grader_version"],
        limits=raw["limits"],
        hints=raw["hints"],
        web_search=raw["web_search"],
        completion_gate=raw["completion_gate"],
        frozen_config_sha256=digest(
            canonical_json(
                {
                    "plan": {key: value for key, value in raw.items() if key != "prepared_at"},
                    "settings": {key: value for key, value in settings.items() if key != "assets"},
                }
            )
        ),
        evidence=[src.evidence(file) for file in (path, settings_path) if file in src.files],
    )
    tasks = [_task(src, key, raw, settings.get("fixtures", {}).get(key)) for key in ids]
    src.verify()
    return Report(manifest=manifest, tasks=tasks, aggregate=aggregate_tasks(tasks))


def _paired_reason(left: TaskRecord, right: TaskRecord, varied: frozenset[str]) -> str | None:
    if left.git_commit != right.git_commit or {
        item.path: item.sha256
        for item in left.evidence
        if item.path.endswith(("/task.json", "/scorer.json", "/recipe.json"))
    } != {
        item.path: item.sha256
        for item in right.evidence
        if item.path.endswith(("/task.json", "/scorer.json", "/recipe.json"))
    }:
        return "different_frozen_inputs"
    a, b = left.attempt(), right.attempt()
    if a is None or b is None:
        return "missing_attempt"
    for name in (
        "provider",
        "model_id",
        "thinking",
        "reasoning_effort",
        "config_sha256",
        "policy_sha256",
        "tools_schema_sha256",
        "prompt_sha256",
        "sandbox_profile",
        "agent_image",
    ):
        value = getattr(a, name)
        other = getattr(b, name)
        if value is None or other is None or (value != other and name not in varied):
            return f"missing_or_different_{name}"
    identity = "fixture_sha256" if a.provider == "fake" else "resolved_revision"
    if getattr(a, identity) is None or getattr(a, identity) != getattr(b, identity):
        return f"missing_or_different_{identity}"
    sa, sb = left.score(), right.score()
    if sa is None or sb is None or sa.resolved is None or sb.resolved is None:
        return "missing_official_verdict"
    if sa.attempt_id != a.attempt_id or sb.attempt_id != b.attempt_id:
        return "unverified_attempt_binding"
    if sa.grader_image != sb.grader_image or sa.grader_version != sb.grader_version:
        return "different_grader"
    return None


def compare(left: Report, right: Report, *, varied: frozenset[str] = frozenset()) -> JSONDict:
    """Return paired counts for strictly comparable observations and explain exclusions."""
    lm, rm = left.manifest, right.manifest
    if not varied.issubset(_VARIATIONS):
        raise ValueError("Unsupported experiment variation")
    controls = [
        name
        for name in (
            "dataset_sha256",
            "suite_revision",
            "revision_verified",
            "split",
            "grader_version",
            "limits",
            "source_sha256",
            "hints",
            "web_search",
            "completion_gate",
        )
        if name not in varied and getattr(lm, name) != getattr(rm, name)
    ]
    if "source_sha256" not in varied and lm.harness_commit != rm.harness_commit:
        controls.append("harness_commit")
    lhs, rhs = ({item.instance_id: item for item in report.tasks} for report in (left, right))
    counts = dict.fromkeys(
        ("both_resolved", "left_only_resolved", "right_only_resolved", "neither_resolved"), 0
    )
    exclusions = {}
    for key in sorted(lhs.keys() & rhs.keys()):
        reason = (
            "different_manifest_controls"
            if controls
            else _paired_reason(lhs[key], rhs[key], varied)
        )
        if reason:
            exclusions[key] = reason
            continue
        ls, rs = lhs[key].score(), rhs[key].score()
        assert ls is not None and rs is not None
        bucket = (
            "both_resolved"
            if ls.resolved and rs.resolved
            else "left_only_resolved"
            if ls.resolved
            else "right_only_resolved"
            if rs.resolved
            else "neither_resolved"
        )
        counts[bucket] += 1
    return {
        "left_experiment_id": lm.experiment_id,
        "right_experiment_id": rm.experiment_id,
        "control_differences": controls,
        "declared_variations": sorted(varied),
        "left_plan_only": sorted(lhs.keys() - rhs.keys()),
        "right_plan_only": sorted(rhs.keys() - lhs.keys()),
        "common_planned": len(lhs.keys() & rhs.keys()),
        "paired_graded": sum(counts.values()),
        "pairs": counts,
        "excluded": exclusions,
    }


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    report = commands.add_parser(
        "report", help="Report from a saved experiment, without model calls"
    )
    report.add_argument("source", type=Path)
    comparison = commands.add_parser("compare", help="Pair two saved experiments by instance ID")
    comparison.add_argument("left", type=Path)
    comparison.add_argument("right", type=Path)
    comparison.add_argument("--vary", action="append", choices=_VARIATIONS, default=[])
    for command_parser in (report, comparison):
        command_parser.add_argument("--output", type=Path, help="Create a new JSON file")
    args = cli.parse_args()
    roots = [args.source] if args.action == "report" else [args.left, args.right]
    try:
        value = (
            read_report(args.source).model_dump(mode="json")
            if args.action == "report"
            else compare(
                read_report(args.left), read_report(args.right), varied=frozenset(args.vary)
            )
        )
        encoded = canonical_json(value) + b"\n"
        if args.output is None:
            sys.stdout.buffer.write(encoded)
        else:
            if args.output.exists() or args.output.is_symlink():
                raise FileExistsError("Report output already exists")
            output = args.output.resolve()
            if any(output.is_relative_to(root.resolve()) for root in roots):
                raise ValueError("Put the report outside the source experiment")
            with output.open("xb") as stream:
                stream.write(encoded)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        cli.exit(2, f"{exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
