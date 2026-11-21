"""Read published receipts without running an experiment or exposing raw secrets."""

import json
import re
from pathlib import Path
from typing import Any

from stellarweaver.domain import JSONDict
from stellarweaver.evals.provenance import digest
from stellarweaver.evals.swebench import ID_PATTERN
from stellarweaver.evals.swebench_contract import Report, aggregate_tasks, compare

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")
_LIMIT = 8 * 1024 * 1024
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_?key|secret|(?:access_|refresh_|web_)?token|password|passwd|"
    r"authorization|cookie|dsn|reasoning_content)$",
    re.I,
)
_SECRET_TEXT = (
    re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?(?:\+\w+)?|https?)://[^\s/@:]+:[^\s/@]+@"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(
        r"""(?i)\b(?:[\w-]*[_-])?(?:api[_-]?key|token|secret|password|passwd|dsn|cookie|authorization)"""
        r"""["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
    ),
)


def redact(value: Any, depth: int = 0) -> Any:
    if depth > 24:
        return "[OMITTED]"
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(key) else redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, depth + 1) for item in value]
    if isinstance(value, str):
        if len(value) <= 128 * 1024 and value.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                if isinstance(structured, (dict, list)):
                    return json.dumps(redact(structured, depth + 1), ensure_ascii=False)
        for pattern in _SECRET_TEXT:
            value = pattern.sub("[REDACTED]", value)
    return value


class ExperimentTooLarge(ValueError):
    pass


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON constant {value}")


def _read(path: Path) -> tuple[JSONDict, str]:
    if path.is_symlink() or path.resolve() != path:
        raise FileNotFoundError(path.name)
    with path.open("rb") as stream:
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ExperimentTooLarge()
    value = json.loads(raw, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Expected a report object")
    return value, digest(raw)


def read_legacy(file: Path) -> JSONDict:
    data, _ = _read(file.parent.resolve() / file.name)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported legacy report")
    result: JSONDict = redact(data)
    return result


def _security(data: JSONDict) -> JSONDict:
    manifest = data["manifest"]
    ids = manifest["instance_ids"]
    cases = data["cases"]
    if (
        manifest.get("schema_version") != 1
        or not isinstance(ids, list)
        or len(ids) > 1000
        or any(not isinstance(name, str) or not _ID.fullmatch(name) for name in ids)
        or len(set(ids)) != len(ids)
        or not isinstance(cases, list)
        or ids != [case["instance_id"] for case in cases]
    ):
        raise ValueError("Invalid security fixture manifest")
    allowed = {"protected", "egress", "public", "repair"}
    results = []
    for case in cases:
        if (
            case["kind"] not in allowed
            or type(case["passed"]) is not bool
            or type(case["unsafe_effect"]) is not bool
            or any(
                type(case[key]) is not int or case[key] < 0
                for key in ("tool_calls", "model_calls", "approvals")
            )
        ):
            raise ValueError("Invalid security fixture observation")
        results.append(
            {
                key: case[key]
                for key in (
                    "instance_id",
                    "kind",
                    "passed",
                    "unsafe_effect",
                    "tool_calls",
                    "model_calls",
                    "approvals",
                    "tool_codes",
                    "oracle",
                    "repair_metrics",
                )
                if key in case
            }
        )
    guarded = [case for case in results if case["kind"] in {"protected", "egress"}]
    normal = [case for case in results if case["kind"] in {"public", "repair"}]
    return {
        "manifest": {
            k: manifest[k]
            for k in ("suite", "instance_ids", "fixture_sha256", "oracle_source_sha256", "model")
        },
        "cases": results,
        "security": {
            "planned": len(guarded),
            "boundary_checks_passed": sum(row["passed"] for row in guarded),
            "unauthorized_effects": sum(row["unsafe_effect"] for row in results),
        },
        "utility": {"planned": len(normal), "passed": sum(row["passed"] for row in normal)},
        "tool_efficiency": {
            key: sum(row[key] for row in results)
            for key in ("tool_calls", "model_calls", "approvals")
        },
    }


def _report(data: JSONDict) -> Report:
    report = Report.model_validate(data)
    if (
        not report.tasks
        or len(report.tasks) > 300
        or report.manifest.instance_ids != [x.instance_id for x in report.tasks]
        or len(set(report.manifest.instance_ids)) != len(report.tasks)
        or any(not re.fullmatch(ID_PATTERN, key) for key in report.manifest.instance_ids)
    ):
        raise ValueError("Inconsistent planned cases")
    return report.model_copy(update={"aggregate": aggregate_tasks(report.tasks)})


class PublishedExperiments:
    def __init__(self, legacy: Path) -> None:
        self.legacy = legacy.parent.resolve() / legacy.name
        self.root = self.legacy.parent / "reports"

    def _entry(self, name: str) -> tuple[JSONDict, str]:
        if not _ID.fullmatch(name) or self.root.is_symlink():
            raise FileNotFoundError()
        return _read(self.root / f"{name}.json")

    def detail(self, name: str) -> JSONDict:
        data, checksum = _read(self.legacy) if name == "legacy" else self._entry(name)
        if name == "legacy":
            if data.get("schema_version") != 1:
                raise ValueError("Unsupported legacy report")
            kind, title = "legacy", data.get("instance_id", name)
        elif data.get("manifest", {}).get("suite") == "swebench_lite":
            validated = _report(data)
            kind, title = "swebench", name
            data = validated.model_dump(mode="json")
        elif data.get("manifest", {}).get("suite") == "local_fixtures":
            kind, title = "security", name
            data = _security(data)
        else:
            raise ValueError("Unsupported report suite")
        return {
            "id": name,
            "kind": kind,
            "title": redact(title),
            "sha256": checksum,
            "report": redact(data),
        }

    def list(self) -> JSONDict:
        names = ["legacy"] if self.legacy.is_file() else []
        if self.root.is_dir() and not self.root.is_symlink():
            names.extend(
                sorted(
                    {
                        path.stem
                        for path in self.root.glob("*.json")
                        if path.stem != "legacy" and _ID.fullmatch(path.stem)
                    }
                )[:24]
            )
        entries, unavailable = [], []
        for name in names:
            try:
                detail = self.detail(name)
                report = detail.pop("report")
                detail["split"] = report.get("manifest", {}).get("split")
                if detail["kind"] == "swebench":
                    detail["planned"] = len(report["tasks"])
                elif detail["kind"] == "security":
                    detail["planned"] = len(report["cases"])
                else:
                    detail["planned"] = 1
                entries.append(detail)
            except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError):
                unavailable.append(name)
        return {"reports": entries, "unavailable": unavailable}

    def comparison(self, left: str, right: str, varied: frozenset[str]) -> JSONDict:
        if left == right:
            raise ValueError("Choose two distinct reports")
        return compare(_report(self._entry(left)[0]), _report(self._entry(right)[0]), varied=varied)
