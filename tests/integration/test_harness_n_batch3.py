"""Synthetic local acceptance for Batch 3."""

import io
import json
import os
import tarfile
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
import pytest

from stellarweaver.config import AppConfig, HarnessConfig, LimitsConfig, PolicyConfig
from stellarweaver.context.navigation import NavigationIndex
from stellarweaver.context.repo_map import RepoMap
from stellarweaver.domain import AssistantReply, ToolCall
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.web.isolation import DockerRunner, Isolation, _archive, git, snapshot_hashes
from stellarweaver.web.remote_runner import RemoteRunner
from stellarweaver.web.snapshots import SnapshotCache, SnapshotMismatch, verify_transfer


def repo(root: Path) -> Path:
    root.mkdir()
    git(root, "init", "--template=")
    return root


def archive_files(snapshot):
    with tarfile.open(fileobj=snapshot, mode="r:") as tar:
        return {
            member.name: (sha256(tar.extractfile(member).read()).hexdigest(), member.mode & 0o755)
            for member in tar
        }


def source_tree(root):
    (root / "keep.txt").write_bytes(b"fixture\n" * 50000)
    (root / "edit.py").write_bytes(b"value = 1\n")
    (root / "remove.py").write_bytes(b"removed = True\n")
    git(root, "add", "--", "keep.txt", "edit.py", "remove.py")


def change_tree(root):
    (root / "edit.py").write_bytes(b"value = 2\n")
    (root / "added.py").write_bytes(b"added = True\n")
    (root / "remove.py").unlink()


def expected_full(root):
    full = io.BytesIO()
    _archive(root, full)
    result = archive_files(full)
    assert {name: item[0] for name, item in result.items()} == snapshot_hashes(root)
    return result


async def test_incremental_remote_upload_checks_tree_and_falls_back(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from stellarweaver.web.broker import create_broker

    root = repo(tmp_path / "repo")
    source_tree(root)
    argv = ["python", "-m", "unittest"]
    sandbox = Isolation("fixture:local", (tuple(argv),), "batch3")
    token = uuid4().hex
    broker = create_broker(sandbox, token)
    client_type = httpx.AsyncClient
    observed = []
    transfers = []

    def local_transport(*args, **kwargs):
        async def track(request):
            transfers.append("delta" if "base" in request.url.params else "full")

        return client_type(
            *args,
            transport=httpx.ASGITransport(app=broker),
            event_hooks={"request": [track]},
            **kwargs,
        )

    async def run_archive(self, archive, command, **kwargs):
        assert command == argv and archive.tell() == 0
        observed.append(archive_files(archive))
        return {"status": "ok", "data": {"process_management": {"cleanup_confirmed": True}}}

    monkeypatch.setattr(httpx, "AsyncClient", local_transport)
    monkeypatch.setattr(DockerRunner, "run_archive", run_archive)
    runner = RemoteRunner(
        root,
        sandbox,
        "http://executor.invalid",
        token,
        pipeline_metrics=True,
        incremental_snapshots=True,
    )
    first = await runner(argv, root)
    assert observed[-1] == expected_full(root)
    change_tree(root)
    second = await runner(argv, root)
    assert observed[-1] == expected_full(root)
    full, delta = (result["data"]["sandbox_timing_ms"] for result in (first, second))
    assert full["snapshot_mode"] == "full" and delta["snapshot_mode"] == "delta"
    assert delta["changed_files_count"] == 3
    assert delta["transfer_bytes"] < full["transfer_bytes"] / 4
    broker = create_broker(sandbox, token)
    third = await runner(argv, root)
    assert third["data"]["sandbox_timing_ms"]["snapshot_mode"] == "full"
    assert transfers == ["full", "delta", "delta", "full"] and len(observed) == 3
    assert observed[-1] == expected_full(root)
    print(
        f"snapshot fixture: full={full['transfer_bytes']} delta={delta['transfer_bytes']}; fallback once"
    )


def test_manifest_checks_modes_and_cache_evicts():
    from stellarweaver.web.snapshots import manifest_digest

    data = b"run\n"
    digest = sha256(data).hexdigest()
    upload = io.BytesIO()
    with tarfile.open(fileobj=upload, mode="w") as tar:
        row = tarfile.TarInfo("script.sh")
        row.mode, row.size = 0o755, len(data)
        tar.addfile(row, io.BytesIO(data))
    with pytest.raises(SnapshotMismatch):
        verify_transfer(
            upload, io.BytesIO(), manifest_digest({"script.sh": (digest, 0o644, len(data))})
        )
    target = io.BytesIO()
    verify_transfer(upload, target, manifest_digest({"script.sh": (digest, 0o755, len(data))}))
    assert archive_files(target)["script.sh"] == (digest, 0o755)
    cache = SnapshotCache(max_bytes=5, max_entries=2)
    cache.put("scope-a", "first", b"123")
    cache.put("scope-b", "second", b"456")
    assert cache.get("scope-a", "first") is None
    assert cache.get("scope-a", "second") is None
    assert cache.get("scope-b", "second") == b"456"


@pytest.mark.asyncio
async def test_cached_docker_image_extracts_delta_tree(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from stellarweaver.web.broker import create_broker

    image = os.getenv("STELLARWEAVER_BATCH3_TEST_IMAGE")
    if not image:
        pytest.skip("Requires an existing pinned Linux Python image")
    script = (
        "import hashlib,json,pathlib; "
        "print(json.dumps({str(p):[hashlib.sha256(p.read_bytes()).hexdigest(),"
        "p.stat().st_mode & 0o777] for p in pathlib.Path('.').rglob('*') if p.is_file()}))"
    )
    argv = ["python", "-I", "-B", "-c", script]
    sandbox = Isolation(image, (tuple(argv),), "batch3-offline")
    token = uuid4().hex
    broker = create_broker(sandbox, token)
    client_type = httpx.AsyncClient

    def local_transport(*args, **kwargs):
        return client_type(*args, transport=httpx.ASGITransport(app=broker), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", local_transport)
    root = repo(tmp_path / "repo")
    source_tree(root)
    runner = RemoteRunner(
        root,
        sandbox,
        "http://executor.invalid",
        token,
        pipeline_metrics=True,
        incremental_snapshots=True,
    )
    for mode in ("full", "delta"):
        if mode == "delta":
            change_tree(root)
        observed = await runner(argv, root)
        assert observed["status"] == "ok", observed["data"].get("code")
        files = {key: tuple(item) for key, item in json.loads(observed["data"]["stdout"]).items()}
        assert files == expected_full(root)
        assert observed["data"]["sandbox_timing_ms"]["snapshot_mode"] == mode


def test_warm_500_file_navigation_reuses_sources_and_invalidates(tmp_path):
    root = repo(tmp_path / "repo")
    for number in range(500):
        (root / f"file_{number:03}.py").write_text(
            "def target_symbol():\n    return 1\n" if number == 499 else "value = 1\n",
            encoding="utf-8",
        )
    lookup = NavigationIndex(RepoMap(root), prefilter=True)
    elapsed = []
    for _ in range(2):
        start = perf_counter()
        result = lookup.query("find_symbol", "target_symbol", None, 5)
        elapsed.append(perf_counter() - start)
        assert [item["path"] for item in result["matches"]] == ["file_499.py"]
    assert result["prefilter_cache_hits"] == 500
    assert elapsed[1] < elapsed[0]
    (root / "file_499.py").write_text("renamed = False\n")
    assert not lookup.query("find_symbol", "target_symbol", None, 5)["matches"]
    print(f"navigation fixture: cold={elapsed[0]:.3f}s warm={elapsed[1]:.3f}s cached=500")


def test_import_neighbors_enter_tight_repomap(tmp_path):
    root = repo(tmp_path / "repo")
    (root / "entry.py").write_text("from deep.package import target\n")
    for number in range(30):
        (root / f"aaa_{number:03}.py").write_text("def noise():\n    pass\n")
    (root / "deep").mkdir()
    (root / "deep" / "package.py").write_text("def target():\n    pass\n")
    (root / "deep" / "caller.py").write_text("import entry\n")
    ranked = []
    for enabled in (False, True):
        view = RepoMap(root, max_indexed_files=3, import_aware=enabled).build(
            explicit_paths=["entry.py"],
            max_tokens=550,
        )
        paths = [item.path for item in view.entries]
        ranked.append(paths)
        for name in ("deep/package.py", "deep/caller.py"):
            assert (name in paths) is enabled
    print(f"repo map fixture: direct neighbors in window 0 -> 2; entries={list(map(len, ranked))}")


def tool(name, options, identity):
    return ToolCall(identity, name, json.dumps(options))


def response(text="", calls=()):
    return AssistantReply(
        text,
        calls,
        stop_reason="tool_calls" if calls else "stop",
        usage={"input_tokens": 80, "output_tokens": 12},
    )


@pytest.mark.postgres
async def test_delegated_usage_is_settled_in_the_parent_web_weekly_ledger(tmp_path):
    dsn = os.getenv("STELLARWEAVER_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("Requires a disposable migrated PostgreSQL test database")
    pytest.importorskip("sqlalchemy")
    from sqlalchemy import text

    from stellarweaver.web.db import engine_for, register, register_member, scoped, submit
    from stellarweaver.web.identity import Member
    from stellarweaver.web.journal import PgJournal

    root = repo(tmp_path / "repo")
    (root / "code.py").write_text("fixture = True\n")
    tenant, workspace, member = uuid4(), str(uuid4()), uuid4()
    engine = engine_for(dsn)
    try:
        register(engine, str(tenant), {workspace: "Batch 3 fixture"})
        register_member(
            engine,
            Member(id=member, tenant=tenant, name="Fixture", role="member", workspace_indexes=(0,)),
            [workspace],
        )
        run, _ = submit(
            engine, str(tenant), workspace, str(uuid4()), "Inspect", "fake", member=str(member)
        )
        journal = PgJournal(
            engine,
            str(tenant),
            run["session_id"],
            run["run_id"],
            root,
            tmp_path / "state",
            member_id=str(member),
            metered=True,
        )
        agent = build_agent(
            AppConfig(
                harness=HarnessConfig(subagent_exploration=True),
                policy=PolicyConfig(preset="read-only"),
            ),
            FakeModel(
                [
                    response(calls=(tool("delegate_exploration", {"goal": "Inspect file"}, "d"),)),
                    response(calls=(tool("read_file", {"path": "code.py"}, "r"),)),
                    response("Located file"),
                    response("Done"),
                ]
            ),
            journal,
            enforce_run_budget=False,
        )
        outcome = await agent.run("Inspect")
        assert outcome.run_status == "completed"
        with scoped(engine, str(tenant)) as conn:
            ledger = conn.execute(
                text("""
                SELECT request_index,tokens,settled FROM sw.model_usage
                WHERE run_id=:run ORDER BY request_index
            """),
                {"run": run["run_id"]},
            ).all()
        assert ledger == [(1, 92, True), (2, 92, True), (3, 92, True), (4, 92, True)]
        assert sum(entry.tokens for entry in ledger) == outcome.input_tokens + outcome.output_tokens
        print("PgJournal fixture: 4 settled rows, 368 tokens including both child requests")
    finally:
        engine.dispose()


async def test_delegation_is_private_and_parent_usage_is_exact(tmp_path):
    root = repo(tmp_path / "repo")
    secret = "fixture internal file contents"
    note = root / "code.py"
    note.write_text(f'def known():\n    return "{secret}"\n')
    calls = []

    def model(request):
        calls.append(request)
        names = {spec["function"]["name"] for spec in request.tools}
        index = len(calls)
        if index in {2, 3, 4}:
            assert names == {
                "list_files",
                "read_file",
                "search_text",
                "find_symbol",
                "find_references",
                "find_related_tests",
            }
            assert not any("parent-only prompt" in item.content for item in request.messages)
        if index == 1:
            return response(
                calls=(tool("delegate_exploration", {"goal": "Locate known"}, "delegate"),)
            )
        if index == 2:
            return response(calls=(tool("read_file", {"path": "code.py"}, "read"),))
        if index == 3:
            return response(
                calls=(
                    tool("find_symbol", {"symbol": "known"}, "symbol"),
                    tool("search_text", {"pattern": "known"}, "search"),
                )
            )
        if index == 4:
            assert any(secret in item.content for item in request.messages)
            return response("Found known in code.py.")
        assert index == 5
        assert secret not in "".join(item.content for item in request.messages)
        [result] = [item for item in request.messages if item.role == "tool"]
        assert result.tool_call_id == "delegate"
        return response("I found the function.")

    with SessionStore(root, tmp_path / "state") as store:
        config = AppConfig(
            harness=HarnessConfig(subagent_exploration=True),
            limits=LimitsConfig(max_model_calls=5, max_tool_calls=4),
        )
        agent = build_agent(config, FakeModel(model), store)
        outcome = await agent.run("parent-only prompt")
        assert outcome.run_status == "completed"
        assert (outcome.model_calls, outcome.tool_calls) == (5, 4)
        assert (outcome.input_tokens, outcome.output_tokens) == (400, 60)
        assert len(agent.results) == 1
        assert agent.results[0].data["evidence"] == [
            {"path": "code.py", "sha256": sha256(note.read_bytes()).hexdigest()}
        ]
        events = store.load_events()
        entries = [e.payload for e in events if e.type == "model.completed"]
        assert [e["request_index"] for e in entries] == list(range(1, 6))
        assert [e.get("purpose") for e in entries] == [
            None,
            "delegate_exploration",
            "delegate_exploration",
            "delegate_exploration",
            None,
        ]
        assert secret not in str(events) and secret not in str(store.load_messages())
        print("delegate fixture: 5 model calls / 4 tool calls, 400+60 tokens, 1 parent tool result")


@pytest.mark.parametrize(
    ("model_max", "tool_max", "expected"),
    [
        (2, 4, (2, 3)),
        (4, 2, (3, 2)),
    ],
)
async def test_delegation_cannot_exceed_parent_call_caps(tmp_path, model_max, tool_max, expected):
    root = repo(tmp_path / "repo")
    (root / "code.py").write_text("value = 1\n")
    request = tool("delegate_exploration", {"goal": "Inspect file"}, "d")
    reads = (
        tool("read_file", {"path": "code.py"}, "r1"),
        tool("read_file", {"path": "code.py"}, "r2"),
    )
    with SessionStore(root, tmp_path / "state") as store:
        config = AppConfig(
            harness=HarnessConfig(subagent_exploration=True),
            limits=LimitsConfig(max_model_calls=model_max, max_tool_calls=tool_max),
        )
        agent = build_agent(
            config,
            FakeModel([response(calls=(request,)), response(calls=reads), response("Done")]),
            store,
        )
        outcome = await agent.run("Delegate")
        assert (outcome.model_calls, outcome.tool_calls) == expected
        child = next(
            event.payload for event in store.load_events() if event.type == "exploration.completed"
        )
        assert child["status"] == "budget_exceeded"
