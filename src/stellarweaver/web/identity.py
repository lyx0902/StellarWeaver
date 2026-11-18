"""G5 account provisioning and server-side access scope."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    tenant: UUID
    name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "member", "viewer"]
    workspace_indexes: tuple[int, ...] = Field(min_length=1)
    subject: str | None = Field(default=None, min_length=1, max_length=255)
    token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    active_limit: int = Field(default=3, ge=1, le=20)
    daily_tokens: int = Field(default=500_000, ge=1, le=20_000_000)


@dataclass(frozen=True)
class Access:
    workspace_ids: tuple[str, ...]
    actor: str
    owner: str | None = None

    def bindings(self) -> dict[str, str | list[str] | None]:
        return {"workspace_ids": list(self.workspace_ids), "actor": self.actor, "owner": self.owner}


def load_members(path: Path, workspace_count: int) -> tuple[Member, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not 1 <= len(raw) <= 100:
        raise ValueError("Account manifest must contain 1 to 100 members")
    members = tuple(Member.model_validate(item) for item in raw)
    keys = [(item.tenant, item.id) for item in members]
    subjects = [item.subject for item in members if item.subject is not None]
    tokens = [item.token_env for item in members if item.token_env is not None]
    if (
        len(set(keys)) != len(keys)
        or len(set(subjects)) != len(subjects)
        or len(set(tokens)) != len(tokens)
    ):
        raise ValueError("Duplicate account identity")
    for item in members:
        if any(index < 0 or index >= workspace_count for index in item.workspace_indexes):
            raise ValueError("Unknown workspace index in account manifest")
    return members
