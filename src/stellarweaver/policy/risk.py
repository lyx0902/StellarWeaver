"""Deterministic capability assessment for a prepared operation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Literal

DS_SEARCH = "ds_search"
POLICY_VERSION = "capabilities/3"
HARD_PATH_ERRORS = frozenset({"PATH_OUTSIDE_WORKSPACE", "PROTECTED_PATH", "INVALID_PATH"})
_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|SIGNING_?KEY|SECRET|TOKEN|PASSWORD|"
    r"PASSWD|CREDENTIALS?|DSN|DATABASE_URL|DB_URL)(?:_|$)"
)
_NONPUBLIC_QUERY = re.compile(
    r"""[\x00-\x1f\x7f]|(?:[a-z]:[\\/]|\\\\)|"""
    r"""(?:^|\s)/(?:home|users|mnt|root|etc)/|"""
    r"""(?:api[_ -]?key|password|secret|token|dsn)\s*[:=]|"""
    r"""(?:https?|postgres(?:ql)?)://[^\s/@]+:[^\s/@]+@""",
    re.IGNORECASE,
)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Risk:
    level: Literal["L0", "L1", "L2", "L3"]
    reason_codes: tuple[str, ...]
    capabilities: tuple[str, ...]
    recommendation: Literal["allow", "ask", "deny"]
    executor: str
    policy_version: str = POLICY_VERSION

    def metadata(self) -> dict[str, Any]:
        # Keep serialized shape identical between memory, SQLite and PostgreSQL.
        return {
            **asdict(self),
            "reason_codes": list(self.reason_codes),
            "capabilities": list(self.capabilities),
        }


def assess(invocation: dict[str, Any], *, read_only: bool = False) -> Risk:
    tool = invocation.get("tool")
    executor = invocation.get("executor", "builtin")
    capabilities: tuple[str, ...]
    if tool == DS_SEARCH:
        query = invocation.get("query")
        capabilities = ("network:public-search-query",)
        if not isinstance(query, str) or not query or _NONPUBLIC_QUERY.search(query):
            return Risk("L3", ("NONPUBLIC_SEARCH_QUERY",), capabilities, "deny", executor)
        return Risk("L0", ("PUBLIC_QUERY_EGRESS",), capabilities, "allow", executor)
    if tool == "mcp_start":
        capabilities = ("process:start", "extension:stdio")
        if any(
            _CREDENTIAL_NAME.search(name.upper()) or name.upper() in {"SSH_AUTH_SOCK", "KUBECONFIG"}
            for name in invocation.get("allow_env", ())
        ):
            return Risk("L3", ("CREDENTIAL_ENVIRONMENT",), capabilities, "deny", executor)
        return Risk("L2", ("EXTENSION_PROCESS",), capabilities, "ask", executor)
    if invocation.get("remote"):
        return Risk(
            "L2", ("EXTENSION_ARGUMENT_HANDOFF",), ("extension:arguments",), "ask", executor
        )
    if tool == "run_command" or invocation.get("authorization_tool") == "run_command":
        if executor == "host" or executor == "builtin":
            return Risk("L2", ("HOST_CODE_EXECUTION",), ("process:host",), "ask", executor)
        return Risk("L1", ("CONFIGURED_EXECUTOR",), ("process:configured",), "ask", executor)
    if read_only:
        return Risk("L0", ("WORKSPACE_READ",), ("workspace:read",), "allow", executor)
    return Risk("L1", ("WORKSPACE_CHANGE",), ("workspace:write",), "ask", executor)
