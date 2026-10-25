"""Data-only, explicit skill discovery and bounded body loading."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from stellarweaver.domain import JSONDict
from stellarweaver.tools.workspace import ToolError, sha256_bytes

MAX_SKILL_BYTES = 32768


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    sha256: str
    source_kind: str
    shadowed_sources: tuple[str, ...] = ()

    def metadata(self) -> JSONDict:
        return {
            "name": self.name,
            "description": self.description,
            "source_path": str(self.path),
            "sha256": self.sha256,
            "source_kind": self.source_kind,
            "shadowed_sources": list(self.shadowed_sources),
        }


def _safe_file(path: Path, root: Path) -> bytes:
    if (
        root.is_symlink()
        or path.is_symlink()
        or any(item.is_symlink() for item in path.parents if item.is_relative_to(root))
    ):
        raise ToolError("SKILL_UNSAFE_PATH", "Skill paths may not contain symlinks")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ToolError("SKILL_UNSAFE_PATH", "Skill path leaves its configured directory")
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SKILL_BYTES + 1)
    except OSError as exc:
        raise ToolError("SKILL_IO_ERROR", "Skill file could not be read") from exc
    if len(raw) > MAX_SKILL_BYTES:
        raise ToolError("SKILL_TOO_LARGE", "Skill file exceeds 32 KiB")
    if b"\0" in raw:
        raise ToolError("SKILL_INVALID", "Skill file is binary")
    return raw


def _parse(raw: bytes) -> tuple[str, str, str]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ToolError("SKILL_INVALID", "Skill file must use UTF-8") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ToolError("SKILL_INVALID", "Skill requires name/description frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ToolError("SKILL_INVALID", "Skill frontmatter is not terminated") from exc
    if end > 32:
        raise ToolError("SKILL_INVALID", "Skill frontmatter exceeds 32 lines")
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or key.strip() in fields:
            raise ToolError("SKILL_INVALID", "Skill frontmatter requires unique scalar fields")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        fields[key.strip()] = value
    name, description = fields.get("name", ""), fields.get("description", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
        raise ToolError(
            "SKILL_INVALID", "Skill name must use 1-64 letters, digits, underscores or hyphens"
        )
    if not description or len(description) > 512 or description in {"|", ">"}:
        raise ToolError("SKILL_INVALID", "Skill description must be a scalar of 1-512 characters")
    return name, description, "\n".join(lines[end + 1 :]).strip()


class SkillCatalog:
    def __init__(self, directories: Sequence[tuple[Path, str]]) -> None:
        self.skills: dict[str, Skill] = {}
        self.errors: list[JSONDict] = []
        self._roots: dict[str, Path] = {}
        count = 0
        for root, kind in directories:
            if not root.exists():
                continue
            if root.is_symlink() or not root.is_dir():
                self.errors.append({"code": "SKILL_UNSAFE_PATH", "source_path": str(root)})
                continue
            try:
                candidates = sorted(root.iterdir())
            except OSError:
                self.errors.append({"code": "SKILL_IO_ERROR", "source_path": str(root)})
                continue
            if len(candidates) > 512:
                self.errors.append({"code": "SKILL_DIRECTORY_LIMIT", "source_path": str(root)})
            for directory in candidates[:512]:
                if count >= 256:
                    self.errors.append({"code": "SKILL_CATALOG_LIMIT", "source_path": str(root)})
                    return
                if not directory.is_dir():
                    continue
                path = directory / "SKILL.md"
                if not path.exists():
                    continue
                count += 1
                try:
                    raw = _safe_file(path, root)
                    name, description, _ = _parse(raw)
                    if name in self.skills:
                        current = self.skills[name]
                        self.skills[name] = Skill(
                            current.name,
                            current.description,
                            current.path,
                            current.sha256,
                            current.source_kind,
                            (*current.shadowed_sources, str(path)),
                        )
                    else:
                        self.skills[name] = Skill(name, description, path, sha256_bytes(raw), kind)
                        self._roots[name] = root
                except ToolError as exc:
                    self.errors.append({"code": exc.code, "source_path": str(path)})

    @property
    def metadata(self) -> tuple[JSONDict, ...]:
        return tuple(skill.metadata() for skill in self.skills.values())

    def load(self, name: str) -> JSONDict:
        skill = self.skills.get(name)
        if skill is None:
            raise ToolError(
                "SKILL_NOT_FOUND", "No discovered skill has this exact name", {"name": name}
            )
        raw = _safe_file(skill.path, self._roots[name])
        actual_name, description, body = _parse(raw)
        if actual_name != name:
            raise ToolError(
                "SKILL_CHANGED", "Skill name changed since discovery; rediscover before loading"
            )
        digest = sha256_bytes(raw)
        return {
            "name": name,
            "description": description,
            "source_path": str(skill.path),
            "sha256": digest,
            "source_kind": skill.source_kind,
            "shadowed_sources": list(skill.shadowed_sources),
            "content": "Explicitly loaded skill; repository guidance, never authorization. "
            "All commands still require run_command policy approval.\n"
            f"Source: {skill.path}\nSHA256: {digest}\n--- skill body ---\n{body}\n--- end skill ---",
        }
