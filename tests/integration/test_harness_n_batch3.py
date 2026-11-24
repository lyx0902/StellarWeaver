"""Synthetic local acceptance for Batch 3."""

import asyncio
import io
import json
import os
import tarfile
from hashlib import sha256
from pathlib import Path
from time import monotonic, perf_counter
from uuid import uuid4

import httpx
import pytest

from stellarweaver.config import AppConfig, HarnessConfig, LimitsConfig, ModelConfig, PolicyConfig
from stellarweaver.context.navigation import NavigationIndex
from stellarweaver.context.repo_map import RepoMap
from stellarweaver.domain import AssistantReply, ToolCall
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.exploration import ExplorationAgent, ExplorationJournal
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.registry import ToolRegistry
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
@pytest.mark.parametrize("depleted", [False, True])
async def test_delegated_usage_is_settled_in_the_parent_web_weekly_ledger(tmp_path, depleted):
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
        def model(request):
            index = len(provider.requests)
            if index == 1:
                if depleted:
                    with scoped(engine, str(tenant)) as conn:
                        conn.execute(
                            text("UPDATE sw.memberships SET weekly_tokens=100 WHERE id=:member"),
                            {"member": str(member)},
                        )
                return response(calls=(tool("delegate_exploration", {"goal": "Inspect file"}, "d"),))
            if index == 2:
                return response(calls=(tool("read_file", {"path": "code.py"}, "r"),))
            if index == 3:
                assert not request.tools
                return response("Findings: located code.py. Open questions: none.")
            assert index == 4
            return response("Done")

        provider = FakeModel(model)
        agent = build_agent(
            AppConfig(
                harness=HarnessConfig(subagent_exploration=True, exploration_max_model_calls=2),
                policy=PolicyConfig(preset="read-only"),
            ),
            provider,
            journal,
            enforce_run_budget=False,
        )
        outcome = await agent.run("Inspect")
        assert outcome.run_status == ("budget_exceeded" if depleted else "completed")
        assert agent.results[0].data["stop_reason"] == ("weekly_quota" if depleted else "safety_cap")
        with scoped(engine, str(tenant)) as conn:
            ledger = conn.execute(
                text("""
                SELECT request_index,tokens,settled FROM sw.model_usage
                WHERE run_id=:run ORDER BY request_index
            """),
                {"run": run["run_id"]},
            ).all()
        assert ledger == [(index, 92, True) for index in range(1, (1 if depleted else 4) + 1)]
        assert sum(entry.tokens for entry in ledger) == outcome.input_tokens + outcome.output_tokens
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
            limits=LimitsConfig(max_model_calls=5, max_tool_calls=5),
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
        (2, 4, (2, 1)),
        (4, 2, (4, 2)),
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
        gather = [response(calls=reads)] if model_max > 2 else []
        provider = FakeModel([
            response(calls=(request,)), *gather, response("Findings: partial."), response("Done")
        ])
        agent = build_agent(config, provider, store)
        outcome = await agent.run("Delegate")
        assert (outcome.model_calls, outcome.tool_calls) == expected
        child = next(
            event.payload for event in store.load_events() if event.type == "exploration.completed"
        )
        assert child["completion_status"] == "partial"
        assert child["stop_reason"] == "parent_budget"
        assert not provider.requests[2 if gather else 1].tools
        assert agent.results[0].content == "Findings: partial."


async def test_web_exploration_runs_past_disabled_parent_limits_and_keeps_32_hashes(tmp_path):
    root = repo(tmp_path / "repo")
    for number in range(33):
        (root / f"part{number}.py").write_bytes(f"value = {number}\n".encode())
    gather = [
        response(calls=tuple(
            tool("read_file", {"path": f"part{number}.py"}, f"read{number}")
            for number in range(start, min(start + 4, 33))
        ))
        for start in range(0, 33, 4)
    ]
    provider = FakeModel([
        response(calls=(tool("delegate_exploration", {"goal": "Collect dependencies"}, "d"),)),
        *gather, response("Findings: " + "x" * 6100), response("Done"),
    ])
    config = AppConfig(
        model=ModelConfig(max_output_tokens=8192),
        limits=LimitsConfig(
            max_model_calls=1, max_tool_calls=1, max_wall_seconds=1,
            max_input_tokens_total=1, max_output_tokens_total=1,
        ),
        harness=HarnessConfig(
            subagent_exploration=True, parallel_read_tools=True, navigation_prefilter=True,
            import_aware_repomap=True, smart_tool_truncation=True, calibrated_context_budget=True,
            sticky_context_checkpoint=True, protect_fresh_tool_output=True,
        ),
    )
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(config, provider, store, enforce_run_budget=False)
        outcome = await agent.run("Inspect")
        [result] = agent.results
        events = store.load_events()
    assert outcome.run_status == "completed"
    assert (outcome.model_calls, outcome.tool_calls) == (12, 34)
    assert (outcome.input_tokens, outcome.output_tokens) == (960, 144)
    assert all(request.max_output_tokens == 8192 for request in provider.requests)
    assert result.data["completion_status"] == result.data["stop_reason"] == "completed"
    assert len(result.content) == 6000 and result.content.endswith("...[truncated]")
    assert result.data["evidence"] == [
        {"path": f"part{number}.py", "sha256": sha256(f"value = {number}\n".encode()).hexdigest()}
        for number in range(32)
    ]
    child = [event.payload for event in events if event.type == "model.started"
             and event.payload.get("purpose") == "delegate_exploration"]
    assert len(child) == 10
    assert child[0]["calibration_factor"] == 1
    assert child[1]["calibration_factor"] < 1


@pytest.mark.parametrize("adaptive", [False, True])
def test_exploration_inherits_adaptive_round_retention(tmp_path, adaptive):
    root = tmp_path / "repo"
    root.mkdir()
    with SessionStore(root, tmp_path / "state") as store:
        parent = build_agent(
            AppConfig(), FakeModel(), store, adaptive_round_retention=adaptive,
        )
        registry = ToolRegistry(root, PolicyEngine("read-only", workspace=root), builtins=False)
        child = ExplorationAgent(
            parent, registry, ExplorationJournal(root, store.directory), "child", monotonic() + 30,
        )
        assert child.adaptive_round_retention is adaptive


@pytest.mark.parametrize("trigger", ["repeated", "errors", "denied", "model_cap", "tool_cap", "timeout"])
async def test_web_exploration_converges_with_one_text_only_summary(tmp_path, trigger, monkeypatch):
    root = repo(tmp_path / "repo")
    (root / "code.py").write_text("value = 1\n", encoding="utf-8")
    read = tool("read_file", {"path": "code.py"}, "r")
    same_read = tool("read_file", {"start_line": 1, "path": "code.py"}, "s")
    gather = [response(calls=(read,))]
    if trigger == "repeated":
        gather.extend([response(calls=(same_read,))] * 2)
    elif trigger == "errors":
        gather.extend(
            response(calls=tuple(
                tool("read_file", {"path": f"missing{i}.py"}, f"missing{i}")
                for i in range(start, start + 3)
            ))
            for start in (0, 3)
        )
    elif trigger == "denied":
        original_decide = PolicyEngine.decide

        async def decide(self, invocation, **kwargs):
            if invocation.get("path") in {"denied1.py", "denied2.py"}:
                return {"decision": "deny", "reason": "Fixture read protection"}
            return await original_decide(self, invocation, **kwargs)

        monkeypatch.setattr(PolicyEngine, "decide", decide)
        for number in (1, 2):
            path = f"denied{number}.py"
            (root / path).write_text("fixture_private_value = 42\n", encoding="utf-8")
            gather.append(response(calls=(tool("read_file", {"path": path}, f"d{number}"),)))

    async def complete(request):
        index = len(provider.requests)
        if index == 1:
            return response(calls=(tool("delegate_exploration", {"goal": "Read it"}, "d"),))
        if index - 2 < len(gather):
            return gather[index - 2]
        if trigger == "timeout" and index == 3:
            await asyncio.sleep(10)
            pytest.fail("Gather request exceeded the child deadline")
        if not request.tools:
            assert request.disable_thinking is True
            assert "code.py:1" in request.messages[-1].content
            assert "value = 1" in request.messages[-1].content
            assert "fixture_private_value" not in request.messages[-1].content
            return response("Findings: inspected code.py. Open questions: remaining files.")
        return response("Done")

    provider = FakeModel(complete)
    harness = HarnessConfig(
        subagent_exploration=True,
        exploration_max_model_calls=2 if trigger == "model_cap" else 20,
        exploration_max_tool_calls=1 if trigger == "tool_cap" else 60,
        exploration_timeout_seconds=2 if trigger == "timeout" else 300,
        exploration_stall_rounds=2,
    )
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(AppConfig(harness=harness), provider, store, enforce_run_budget=False)
        outcome = await agent.run("Inspect")
        [completed] = [
            event.payload for event in store.load_events() if event.type == "exploration.completed"
        ]
        if trigger == "denied":
            statuses = [
                event.payload["status"] for event in store.load_events()
                if event.type == "exploration.tool" and event.payload["event"] == "tool.completed"
            ]
            assert statuses == ["ok", "denied", "denied"]
    assert outcome.run_status == "completed"
    assert sum(not request.tools for request in provider.requests) == 1
    assert agent.results[0].content.startswith("Findings: inspected code.py.")
    for data in (agent.results[0].data, completed):
        assert data["completion_status"] == "partial"
        assert data["stop_reason"] == (
            "stalled" if trigger in {"repeated", "errors", "denied"} else "safety_cap"
        )


def test_exploration_completion_metadata_is_visible_in_web_events():
    app = pytest.importorskip("stellarweaver.web.app", exc_type=ImportError)
    started = app.public_event({
        "type": "exploration.started",
        "payload": {
            "exploration_id": "explore",
            "goal": "Audit runtime loop",
            "paths": ["src/stellarweaver/runtime"],
            "private": "hidden",
        },
    })["payload"]
    assert started == {
        "exploration_id": "explore",
        "goal": "Audit runtime loop",
        "paths": ["src/stellarweaver/runtime"],
    }
    tool_evt = app.public_event({
        "type": "exploration.tool",
        "payload": {
            "exploration_id": "explore",
            "event": "tool.completed",
            "tool_name": "read_file",
            "status": "ok",
            "operation_id": "op-1",
            "private": "hidden",
        },
    })["payload"]
    assert tool_evt == {
        "exploration_id": "explore",
        "event": "tool.completed",
        "tool_name": "read_file",
        "status": "ok",
        "operation_id": "op-1",
    }
    for status, reason in (("completed", "completed"), ("partial", "stalled"), ("cancelled", "cancelled")):
        completion = {"completion_status": status, "stop_reason": reason}
        child = app.public_event({
            "type": "exploration.completed",
            "payload": {"exploration_id": "explore", "model_calls": 3, "private": "hidden", **completion},
        })["payload"]
        assert child == {"exploration_id": "explore", "model_calls": 3, **completion}
        receipt = app.public_event({
            "type": "tool.completed",
            "payload": {
                "status": "cancelled" if status == "cancelled" else "ok",
                "tool_name": "delegate_exploration",
                "data": completion,
            },
        })["payload"]
        assert {key: receipt[key] for key in completion} == completion


async def test_exploration_rescues_evidence_on_failure_and_retries_silently_when_streaming(tmp_path):
    from stellarweaver.providers.base import ProviderError

    root = repo(tmp_path / "repo")
    (root / "core.py").write_text("TOKEN = 42\n", encoding="utf-8")
    deltas: list[str] = []
    calls = 0

    async def complete(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(
                calls=(
                    tool(
                        "delegate_exploration",
                        {"goal": "Audit core.py", "paths": ["core.py"]},
                        "d",
                    ),
                )
            )
        if calls == 2:
            raise ProviderError("Model request timed out; no response was committed", retryable=True)
        if calls == 3:
            return response(calls=(tool("read_file", {"path": "core.py"}, "r"),))
        if calls == 4:
            raise ProviderError("Model API returned HTTP 400; check provider configuration", retryable=False)
        if calls == 5:
            assert not request.tools and request.disable_thinking is True
            assert "TOKEN = 42" in request.messages[-1].content
            return response("Findings: core.py defines TOKEN = 42.")
        return response("Parent final reply")

    provider = FakeModel(complete)
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(
            AppConfig(harness=HarnessConfig(subagent_exploration=True)),
            provider,
            store,
            enforce_run_budget=False,
            on_delta=lambda delta: deltas.append(delta.text),
        )
        outcome = await agent.run("Inspect core.py")
        events = store.load_events()
        [started] = [e.payload for e in events if e.type == "exploration.started"]
        [completed] = [e.payload for e in events if e.type == "exploration.completed"]
        failed = [e.payload for e in events if e.type == "model.failed"]
    assert outcome.run_status == "completed"
    assert started["goal"] == "Audit core.py"
    assert started["paths"] == ["core.py"]
    assert completed["completion_status"] == "partial"
    assert completed["stop_reason"] == "exploration_failed"
    assert "TOKEN = 42" in agent.results[0].content
    assert "".join(deltas) == "Parent final reply"
    assert any("timed out" in item.get("message", "") for item in failed)


async def test_exploration_pagination_advances_and_cancel_propagates(tmp_path):
    root = repo(tmp_path / "repo")
    (root / "code.py").write_text("line\n" * 240, encoding="utf-8")
    waiting = asyncio.Event()

    async def complete(request):
        index = len(provider.requests)
        assert request.tools  # Exploration should still be gathering after three distinct pages.
        if index == 1:
            return response(calls=(tool("delegate_exploration", {"goal": "Read pages"}, "d"),))
        if index <= 4:
            start = (index - 2) * 80 + 1
            return response(calls=(tool(
                "read_file", {"start_line": start, "end_line": start + 79, "path": "code.py"}, "r"
            ),))
        waiting.set()
        await asyncio.Event().wait()
        pytest.fail("The delegated request should have been cancelled")

    provider = FakeModel(complete)
    with SessionStore(root, tmp_path / "state") as store:
        config = AppConfig(harness=HarnessConfig(subagent_exploration=True))
        agent = build_agent(config, provider, store, enforce_run_budget=False)
        task = asyncio.create_task(agent.run("Read"))
        await asyncio.wait_for(waiting.wait(), 10)
        task.cancel()
        outcome = await task
        child = next(e.payload for e in store.load_events() if e.type == "exploration.completed")
    assert outcome.run_status == "cancelled"
    assert outcome.tool_calls == 4
    assert len(provider.requests) == 5
    assert child["tool_calls"] == 3
    assert child["completion_status"] == child["stop_reason"] == "cancelled"
    assert agent.results[0].data["completion_status"] == "cancelled"


@pytest.mark.parametrize("resource", ["input", "output"])
async def test_exploration_reserves_summary_within_parent_token_budgets(tmp_path, resource):
    root = repo(tmp_path / "repo")
    (root / "code.py").write_text("evidence = 42\n", encoding="utf-8")

    def model(request):
        index = len(provider.requests)
        if index == 1:
            return response(calls=(tool("delegate_exploration", {"goal": "Inspect"}, "d"),))
        if index == 2:
            return AssistantReply(
                tool_calls=(tool("read_file", {"path": "code.py"}, "r"),),
                stop_reason="tool_calls",
                usage={
                    "input_tokens": 46_000 if resource == "input" else 80,
                    "output_tokens": request.max_output_tokens if resource == "output" else 12,
                },
            )
        if index == 3:
            assert request.tools == ()
            assert "evidence = 42" in request.messages[-1].content
            if resource == "output":
                assert request.max_output_tokens <= limits.max_output_tokens_total - (
                    12 + provider.requests[1].max_output_tokens
                )
            return response("Findings: evidence is 42. Open questions: none.")
        return response("Done")

    limits = LimitsConfig(max_input_tokens_total=50_000, max_output_tokens_total=4096)
    config = AppConfig(
        model=ModelConfig(max_output_tokens=4096), limits=limits,
        harness=HarnessConfig(subagent_exploration=True),
    )
    provider = FakeModel(model)
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(config, provider, store)
        outcome = await agent.run("Inspect")
    assert outcome.input_tokens <= limits.max_input_tokens_total
    assert outcome.output_tokens <= limits.max_output_tokens_total
    assert agent.results[0].data["completion_status"] == "partial"
    assert agent.results[0].data["stop_reason"] == "parent_budget"
    assert agent.results[0].content.startswith("Findings:")


@pytest.mark.parametrize("enforce_budget", [False, True])
async def test_web_explorations_run_up_to_three_concurrently_with_isolated_accounting(
    tmp_path, enforce_budget
):
    root = repo(tmp_path / "repo")
    for idx in range(5):
        (root / f"mod_{idx}.py").write_text(f"VALUE_{idx} = {idx}\n", encoding="utf-8")

    active = 0
    peak = 0
    parent_done = False

    async def complete(request):
        nonlocal active, peak, parent_done
        user_text = next((m.content for m in request.messages if m.role == "user"), "")
        if not parent_done and "Audit all 5 modules" in user_text:
            parent_done = True
            return AssistantReply(
                tool_calls=tuple(
                    tool(
                        "delegate_exploration",
                        {"goal": f"Inspect module {idx}", "paths": [f"mod_{idx}.py"]},
                        f"exp-{idx}",
                    )
                    for idx in range(5)
                ),
                stop_reason="tool_calls",
                usage={"input_tokens": 100, "output_tokens": 20},
            )
        if user_text.startswith("Inspect module "):
            idx = int(user_text.removeprefix("Inspect module ").strip())
            has_tool_result = any(m.role == "tool" for m in request.messages)
            if not has_tool_result:
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(0.08)
                finally:
                    active -= 1
                return AssistantReply(
                    tool_calls=(tool("read_file", {"path": f"mod_{idx}.py"}, f"read-{idx}"),),
                    stop_reason="tool_calls",
                    usage={"input_tokens": 200 + idx, "output_tokens": 30 + idx},
                )
            return AssistantReply(
                text=f"Summary for mod_{idx}: VALUE_{idx} = {idx}",
                stop_reason="stop",
                usage={"input_tokens": 250 + idx, "output_tokens": 40 + idx},
            )
        return AssistantReply(
            text="All 5 modules audited.",
            stop_reason="stop",
            usage={"input_tokens": 300, "output_tokens": 50},
        )

    provider = FakeModel(complete)
    config = AppConfig(
        harness=HarnessConfig(subagent_exploration=True, parallel_read_tools=True)
    )
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(config, provider, store, enforce_run_budget=enforce_budget)
        outcome = await agent.run("Audit all 5 modules")
        events = store.load_events()
        started_requests = [e.payload["request_index"] for e in events if e.type == "model.started"]
        completed_children = [e.payload for e in events if e.type == "exploration.completed"]
    assert outcome.run_status == "completed"
    assert peak == (1 if enforce_budget else 3)
    assert len(started_requests) == len(set(started_requests)) == 12
    assert [r.data["paths"] for r in agent.results] == [[f"mod_{idx}.py"] for idx in range(5)]
    assert [r.content for r in agent.results] == [
        f"Summary for mod_{idx}: VALUE_{idx} = {idx}" for idx in range(5)
    ]
    by_path = {item["paths"][0]: item for item in completed_children}
    for idx in range(5):
        child = by_path[f"mod_{idx}.py"]
        assert child["model_calls"] == 2
        assert child["input_tokens"] == (200 + idx) + (250 + idx)
        assert child["output_tokens"] == (30 + idx) + (40 + idx)


async def test_exploration_preserves_length_truncated_draft_and_wide_observations(tmp_path):
    root = repo(tmp_path / "repo")
    marker = "DEF_DRIVE_TAIL_MARKER = 999"
    (root / "big.py").write_text("# padding\n" * 160 + marker + "\n", encoding="utf-8")
    step = 0

    async def complete(request):
        nonlocal step
        step += 1
        if step == 1:
            return response(
                calls=(tool("delegate_exploration", {"goal": "Trace big.py", "paths": ["big.py"]}, "d"),)
            )
        if step == 2:
            return response(calls=(tool("read_file", {"path": "big.py"}, "r"),))
        if step == 3:
            return AssistantReply(
                text="Draft finding: big.py has padding before main entry...",
                stop_reason="length",
            )
        if step == 4:
            assert not request.tools and request.disable_thinking is True
            body = request.messages[-1].content
            assert "Partial draft before truncation:" in body
            assert "Draft finding: big.py has padding" in body
            assert marker in body
            return response("Final concise summary including DEF_DRIVE_TAIL_MARKER.")
        return response("Parent done.")

    provider = FakeModel(complete)
    with SessionStore(root, tmp_path / "state") as store:
        agent = build_agent(
            AppConfig(harness=HarnessConfig(subagent_exploration=True)),
            provider,
            store,
            enforce_run_budget=False,
        )
        outcome = await agent.run("Trace big.py")
    assert outcome.run_status == "completed"
    assert "DEF_DRIVE_TAIL_MARKER" in agent.results[0].content
