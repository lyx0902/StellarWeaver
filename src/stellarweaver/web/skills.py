"""Skill sources explicitly configured on the workbench server."""

import os
from collections.abc import Sequence
from pathlib import Path
from threading import Lock

from stellarweaver.domain import JSONDict
from stellarweaver.extensions.skills import SkillCatalog
from stellarweaver.tools.workspace import ToolError, sha256_bytes


def project_skill_root(workspace: Path) -> Path:
    root = workspace / ".stellarweaver" / "skills"
    if any(p.is_symlink() or p.is_junction() for p in (root.parent, root)):
        raise ToolError("SKILL_UNSAFE_PATH", "项目技能目录不能使用链接。")
    return root


def configured_skill_roots() -> tuple[tuple[str, Path], ...]:
    return tuple(
        (source, Path(value).absolute())
        for source, name in (
            ("Agents", "SW_WEB_AGENT_SKILLS_ROOT"),
            ("Agents / skills-codex", "SW_WEB_NESTED_SKILLS_ROOT"),
            ("Codex", "SW_WEB_CODEX_SKILLS_ROOT"),
        )
        if (value := os.getenv(name))
        and (name != "SW_WEB_NESTED_SKILLS_ROOT" or Path(value).exists())
    )


class SkillLibrary:
    def __init__(self, roots: Sequence[tuple[str, Path]]) -> None:
        self.roots = tuple((name, root.absolute()) for name, root in roots)
        self._lock = Lock()
        self._entries: dict[str, tuple[SkillCatalog, JSONDict]] = {}
        self._errors: list[JSONDict] = []
        self._scanned = False

    def list(self, *, refresh: bool = False) -> JSONDict:
        with self._lock:
            if refresh or not self._scanned:
                self._scan()
            return {
                "sources": [source for source, _ in self.roots],
                "skills": sorted(
                    (item for _, item in self._entries.values()),
                    key=lambda item: (item["name"].casefold(), item["source"]),
                ),
                "errors": self._errors,
            }

    def detail(self, skill_id: str) -> JSONDict:
        with self._lock:
            if not self._scanned:
                self._scan()
            found = self._entries.get(skill_id)
        if found is None:
            raise ToolError("SKILL_NOT_FOUND", "找不到该技能，请刷新目录。")
        catalog, item = found
        body = catalog.read(item["name"])
        return {**item, **{k: body[k] for k in ("description", "sha256", "content")}}

    def pinned(self, selected: Sequence[JSONDict]) -> tuple[JSONDict, ...]:
        loaded = []
        for item in selected:
            skill = self.detail(item["id"])
            if skill["sha256"] != item["sha256"]:
                raise ToolError("SKILL_VERSION_CHANGED", "技能版本已变更，请重新选择。")
            loaded.append(skill)
        return tuple(loaded)

    def _scan(self) -> None:
        entries: dict[str, tuple[SkillCatalog, JSONDict]] = {}
        errors: list[JSONDict] = []
        for source, root in self.roots:
            if not root.is_dir() or root.is_symlink():
                errors.append({"source": source, "path": ".", "code": "SKILL_SOURCE_UNAVAILABLE"})
                continue
            catalog = SkillCatalog(((root, source),))
            for error in catalog.errors:
                errors.append(
                    {
                        "source": source,
                        "path": Path(error["source_path"]).relative_to(root).as_posix(),
                        "code": error["code"],
                    }
                )
            for skill in catalog.skills.values():
                path = skill.path.relative_to(root).as_posix()
                skill_id = sha256_bytes(f"{source}\0{path}".encode())
                entries[skill_id] = (
                    catalog,
                    {
                        "id": skill_id,
                        "name": skill.name,
                        "description": skill.description,
                        "path": path,
                        "source": source,
                        "sha256": skill.sha256,
                        "shadowed_paths": [
                            Path(other).relative_to(root).as_posix()
                            for other in skill.shadowed_sources
                        ],
                    },
                )
        self._entries, self._errors, self._scanned = entries, errors, True
