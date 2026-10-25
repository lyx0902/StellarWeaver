"""Skill sources remain explicit, scoped and subordinate to tool authorization."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from stellarweaver.domain import ToolCall
from stellarweaver.extensions import ExtensionManager
from stellarweaver.extensions.mcp import tool_name
from stellarweaver.extensions.skills import SkillCatalog
from stellarweaver.policy import PolicyEngine
from stellarweaver.session.locking import workspace_write_lock
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry


def skill(
    root: Path, name: str = "python-tests", body: str = "BODY_MARKER: inspect unittest"
) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Choose real Python checks\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


async def test_default_manager_has_no_events_no_extra_tools_and_does_not_read_project_skills(
    tmp_path: Path,
) -> None:
    skill(tmp_path / ".stellarweaver/skills")
    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(tmp_path, PolicyEngine(workspace=tmp_path))
        async with ExtensionManager(registry, store) as manager:
            assert not manager.skill_catalog
            assert registry.extension_context is None
            assert len(registry.schemas()) == 8
        assert not store.load_events()


async def test_trusted_project_metadata_precedes_explicit_user_skill_and_body_is_explicit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    project = skill(root / ".stellarweaver/skills")
    user = skill(tmp_path / "user-skills", body="USER_BODY_MARKER")
    with SessionStore(root, tmp_path / "state") as store:
        registry = ToolRegistry(root, PolicyEngine("read-only", workspace=root))
        async with ExtensionManager(
            registry, store, trusted=True, user_skill_dirs=(tmp_path / "user-skills",)
        ) as manager:
            metadata = manager.skill_catalog[0]
            assert metadata["source_path"] == str(project)
            assert metadata["shadowed_sources"] == [str(user)]
            assert "BODY_MARKER" not in registry.extension_context.content  # type: ignore[union-attr]
            result = await registry.dispatch(
                ToolCall("load", "load_skill", '{"name":"python-tests"}')
            )
            assert result.status == "ok"
            assert "BODY_MARKER" in result.content
            assert "USER_BODY_MARKER" not in result.content
            assert result.data["sha256"] == hashlib.sha256(project.read_bytes()).hexdigest()
            assert "never authorization" in result.content
            assert any(event.type == "skill.loaded" for event in store.load_events())
            denied = await registry.dispatch(
                ToolCall("unsafe", "run_command", '{"argv":["python","-c","pass"]}')
            )
            assert denied.status == "denied"
        assert "load_skill" not in registry._tools


async def test_explicit_user_skill_dir_can_be_loaded_without_workspace_trust(
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "user"
    skill(user_root)
    workspace = tmp_path / "work"
    workspace.mkdir()
    with SessionStore(workspace, tmp_path / "state") as store:
        registry = ToolRegistry(workspace, PolicyEngine(workspace=workspace))
        async with ExtensionManager(registry, store, user_skill_dirs=(user_root,)) as manager:
            assert manager.skill_catalog[0]["source_kind"] == "explicit_user"
            assert (
                await registry.dispatch(ToolCall("a", "load_skill", '{"name":"python-tests"}'))
            ).status == "ok"


@pytest.mark.parametrize(
    "body",
    [
        "missing frontmatter",
        "---\nname: wrong name\ndescription: desc\n---\n",
        "---\nname: okay\ndescription: |\n---\n",
        "x" * 40000,
    ],
    ids=["no-frontmatter", "invalid-name", "non-scalar-description", "oversize"],
)
def test_broken_and_oversize_skills_report_errors_without_becoming_catalog_entries(
    tmp_path: Path, body: str
) -> None:
    path = tmp_path / "bad/SKILL.md"
    path.parent.mkdir()
    path.write_text(body)
    catalog = SkillCatalog(((tmp_path, "explicit_user"),))
    assert not catalog.metadata
    assert catalog.errors


async def test_workspace_lock_blocks_mutation_but_allows_read_and_does_not_overwrite(
    tmp_path: Path,
) -> None:
    (tmp_path / "baseline.txt").write_text("user")
    registry = ToolRegistry(tmp_path, PolicyEngine("workspace-edit", workspace=tmp_path))
    with workspace_write_lock(tmp_path):
        blocked = await registry.dispatch(
            ToolCall("write", "create_file", '{"path":"new.txt","content":"bad"}')
        )
        assert blocked.status == "blocked"
        assert blocked.data["code"] == "WORKSPACE_BUSY"
        assert not (tmp_path / "new.txt").exists()
        read = await registry.dispatch(ToolCall("read", "read_file", '{"path":"baseline.txt"}'))
        assert read.status == "ok"
    success = await registry.dispatch(
        ToolCall("write", "create_file", '{"path":"new.txt","content":"good"}')
    )
    assert success.status == "ok"


def test_normalized_remote_names_handle_collisions_unicode_and_length() -> None:
    names = {
        tool_name("a.b", "工具" * 100),
        tool_name("a/b", "工具" * 100),
        tool_name("a.b", "different"),
        tool_name("a.b", "工具" * 100 + "a"),
    }
    assert len(names) == 4
    assert all(len(name) <= 64 and name.isascii() and name.startswith("mcp__") for name in names)


def test_external_json_schema_refs_are_rejected_before_any_request(tmp_path: Path) -> None:
    from stellarweaver.extensions import RemoteArguments

    registry = ToolRegistry(tmp_path, PolicyEngine(workspace=tmp_path))

    async def prepare(*args: object) -> object:
        raise AssertionError("Schema registration must fail first")

    with pytest.raises(ValueError, match="External JSON Schema references"):
        registry.register(
            "mcp__test",
            "bad reference",
            RemoteArguments,
            prepare,
            schema={"$ref": "https://example.com/schema.json"},
        )  # type: ignore[arg-type]


def test_large_structured_mcp_result_is_bounded_and_omission_is_explicit() -> None:
    from mcp.types import CallToolResult

    from stellarweaver.extensions.mcp import normalize_result

    value = {"large": "中文" * 10000}
    result = normalize_result(CallToolResult(content=[], structuredContent=value), limit=1024)
    projected = result["data"]["structured_content"]
    assert projected["truncated"]
    assert projected["original_preserved"] is False
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    assert projected["original_utf8_bytes"] == len(raw)
    assert projected["sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(json.dumps(result).encode()) < 1024


async def test_remote_large_structured_artifact_has_hard_size_limit(tmp_path: Path) -> None:
    from mcp.types import CallToolResult

    from stellarweaver.extensions.mcp import ServerConfig

    class Connection:
        config = ServerConfig(server_id="bounded", command="unused")

        async def call(self, name: str, args: object) -> CallToolResult:
            return CallToolResult(content=[], structuredContent={"large": "中文" * 400000})

    with SessionStore(tmp_path, tmp_path / "state") as store:
        registry = ToolRegistry(
            tmp_path, PolicyEngine(workspace=tmp_path), artifacts_dir=store.artifacts_dir
        )
        manager = ExtensionManager(registry, store)
        manager._register_remote(
            Connection(), "large", "mcp__bounded", "bounded result", {"type": "object"}, True
        )  # type: ignore[arg-type]
        result = await registry.dispatch(ToolCall("large", "mcp__bounded", "{}"))
        assert result.status == "ok"
        projection = result.data["structured_content"]
        assert projection["truncated"] and not projection["original_preserved"]
        assert projection["original_utf8_bytes"] > 2_000_000
        artifact = Path(result.artifact_ids[0])
        assert (await asyncio.to_thread(artifact.stat)).st_size <= 1048576
        assert (
            hashlib.sha256(await asyncio.to_thread(artifact.read_bytes)).hexdigest()
            == projection["artifact"]["sha256"]
        )
        assert len(json.dumps(result.data).encode()) < 2048
