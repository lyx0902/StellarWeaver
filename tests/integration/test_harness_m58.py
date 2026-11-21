"""Development controls and repair with only local fixtures."""

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from stellarweaver.config import AppConfig, ExecutionConfig, HarnessConfig, PolicyConfig
from stellarweaver.context.navigation import IssueSignals
from stellarweaver.domain import AssistantReply, JSONDict, ModelRequest, ToolCall, ToolResult
from stellarweaver.policy import PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.repair import classify
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.navigation import register_navigation
from stellarweaver.tools.registry import ToolRegistry
from stellarweaver.tools.workspace import sha256_bytes
from stellarweaver.web.isolation import git


def action(index: int, name: str, **args: str | list[str]) -> AssistantReply:
    return AssistantReply(
        tool_calls=(ToolCall(str(index), name, json.dumps(args)),), stop_reason="tool_calls"
    )


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_bytes(b"number = 1\n")
    (root / "test_calc.py").write_text(
        "from unittest import TestCase\nfrom calc import number\n"
        "class Fixture(TestCase):\n    def test_number(self):\n        self.assertEqual(number, 2)\n",
        encoding="utf-8",
    )
    (root / ".env").write_bytes(b"FIXTURE=synthetic-only\n")
    git(root, "init", "--template=")
    return root


async def test_missing_configured_sandbox_does_not_run_on_host(tmp_path: Path) -> None:
    registry = ToolRegistry(
        tmp_path,
        PolicyEngine("ask", workspace=tmp_path),
        execution=ExecutionConfig(backend="docker", image="sha256:" + "a" * 64, allow_argv=True),
    )
    result = await registry.dispatch(ToolCall("id", "run_command", '{"argv":["python","-V"]}'))
    assert result.status == "error"
    assert result.data["code"] == "SANDBOX_UNAVAILABLE"


@pytest.fixture
def image() -> str:
    pinned = os.getenv("STELLARWEAVER_M5_TEST_IMAGE")
    if not pinned:
        pytest.skip("Requires a cached Linux Python image pinned by ID")
    return pinned


async def test_readonly_docker_profile_through_shared_runtime(
    tmp_path: Path, image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = await asyncio.to_thread(workspace, tmp_path)
    (tmp_path / "outside.txt").write_bytes(b"workspace sibling")
    monkeypatch.setenv("STELLARWEAVER_DATABASE_DSN", "fixture-not-for-container")
    script = r"""
import os
from pathlib import Path
assert os.getuid() == 65534
assert os.listdir('/sys/class/net') == ['lo']
assert not Path('/var/run/docker.sock').exists()
assert not Path('.git').exists()
assert not Path('.env').exists()
assert not Path('../outside.txt').exists()
assert 'STELLARWEAVER_API_KEY' not in os.environ
assert 'STELLARWEAVER_DATABASE_DSN' not in os.environ
if Path('/sys/fs/cgroup/pids.max').is_file():
    assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '128'
    assert int(Path('/sys/fs/cgroup/memory.max').read_text().strip()) == 512 * 1024 ** 2
assert Path('calc.py').read_text() == 'number = 1\n'
try:
    Path('calc.py').write_text('isolated scratch')
except PermissionError:
    print('readonly confirmed')
else:
    raise AssertionError('readonly source was writable')
"""
    config = AppConfig(
        execution=ExecutionConfig(
            backend="docker", image=image, profile="sandbox-read-only", allow_argv=True
        ),
    )

    async def approve(invocation: JSONDict) -> str:
        assert invocation["executor"] == "configured"
        assert invocation["command_boundary"]
        return "once"

    with SessionStore(root, tmp_path / "journal") as store:
        model = FakeModel(
            [action(1, "run_command", argv=["python", "-c", script]), AssistantReply("Checked")]
        )
        run = build_agent(config, model, store, interactive=True, approval=approve, stream=False)
        outcome = await run.run("Inspect the read-only snapshot.")
        (result,) = store.load_results()
        assert outcome.verification_status == "passed", result
        assert result.data["sandbox_profile"] == "sandbox-read-only"
        assert result.data["image_id"] == image
        assert result.data["network"] == "none"
        assert len(result.data["snapshot_sha256"]) == 64
        assert result.data["limits"]["pids"] == 128
        assert result.data["process_management"]["cleanup_confirmed"]
        assert "readonly confirmed" in result.data["stdout"]
        assert (root / "calc.py").read_bytes() == b"number = 1\n"
        assert (tmp_path / "outside.txt").read_bytes() == b"workspace sibling"


async def test_development_tools_real_assertion_edit_and_recheck(
    tmp_path: Path, image: str
) -> None:
    root = await asyncio.to_thread(workspace, tmp_path)
    config = AppConfig(
        execution=ExecutionConfig(backend="docker", image=image),
        harness=HarnessConfig(development_tools=True, repair_controller="feedback"),
        policy=PolicyConfig(preset="workspace-edit"),
    )
    replies = [
        action(1, "discover_tests"),
        action(2, "check_syntax", target="calc.py"),
        action(3, "run_tests", target="test_calc.py"),
        action(
            4,
            "edit_file",
            path="calc.py",
            old_text="number = 1",
            new_text="number = 2",
            expected_sha256=sha256_bytes(b"number = 1\n"),
        ),
        action(5, "run_tests", target="test_calc.py"),
        AssistantReply("Verified"),
    ]
    with SessionStore(root, tmp_path / "journal") as store:
        model = FakeModel(replies)
        run = build_agent(config, model, store, allow_sandbox_development=True, stream=False)
        outcome = await run.run("Repair the test assertion.")
        results = store.load_results()
        assert outcome.verification_status == "passed", results
        assert [row.status for row in results] == ["ok", "ok", "error", "ok", "ok"]
        assert json.loads(results[0].data["stdout"])["tests"][0]["name"] == "test_number"
        assert results[2].data["exit_code"] == 1
        assert results[4].data["exit_code"] == 0
        assert results[2].data["snapshot_sha256"] != results[4].data["snapshot_sha256"]
        assert results[4].data["diagnostic"]["action"] == "run_tests"
        events = [
            event.payload for event in store.load_events() if event.type == "repair.diagnostic"
        ]
        assert events[1]["category"] == "ASSERTION_FAILED"
        assert events[1]["operation_id"] == results[2].operation_id
        assert events[-1]["category"] == "CHECK_PASSED" and events[-1]["candidate_edit_sha256"]
        assert "ASSERTION_FAILED" in "\n".join(
            message.content for message in model.requests[3].messages
        )


@pytest.fixture
def verification_image() -> str:
    pinned = os.getenv("STELLARWEAVER_VERIFICATION_TEST_IMAGE")
    if not pinned:
        pytest.skip("Requires the pinned StellarWeaver verification image")
    return pinned


@pytest.fixture
def verification_checkout(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    checkout = tmp_path / "project"
    test_path = checkout / "tests" / "integration" / "test_public_runtime.py"
    test_path.parent.mkdir(parents=True)
    shutil.copyfile(source / "pyproject.toml", checkout / "pyproject.toml")
    shutil.copyfile(source / "tests" / "integration" / test_path.name, test_path)
    shutil.copytree(
        source / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    git(checkout, "init", "--template=")
    return checkout


async def test_verification_image_runs_project_async_tests_and_ruff(
    tmp_path: Path, verification_image: str, verification_checkout: Path
) -> None:
    config = AppConfig(
        execution=ExecutionConfig(backend="docker", image=verification_image),
        harness=HarnessConfig(development_tools=True, repair_controller="feedback"),
    )
    target = "tests/integration/test_public_runtime.py"
    with SessionStore(verification_checkout, tmp_path / "journal") as store:
        model = FakeModel(
            [
                action(1, "run_tests", target=target, framework="pytest"),
                action(2, "run_linter", target=target),
                AssistantReply("Checked."),
            ]
        )
        agent = build_agent(config, model, store, allow_sandbox_development=True, stream=False)
        outcome = await agent.run("Check the actual project tests and lint.")
        tests, lint = store.load_results()

    assert outcome.verification_status == "passed", (tests, lint)
    assert "3 passed" in tests.data["stdout"], tests
    assert "All checks passed" in lint.data["stdout"], lint
    for result in (tests, lint):
        assert result.status == "ok" and result.data["exit_code"] == 0
        assert result.data["image_id"] == verification_image
        assert result.data["network"] == "none"
        assert result.data["process_management"]["cleanup_confirmed"]


async def test_navigation_top_k_aliases_related_tests_and_cache(tmp_path: Path) -> None:
    root = tmp_path / "src" / "pkg"
    root.mkdir(parents=True)
    target = root / "service.py"
    target.write_text("class Calculator:\n    def price(self): return 2\n", encoding="utf-8")
    (root / "__init__.py").write_text("from .service import Calculator as C\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from pkg.service import Calculator as Alias\ndef test_price():\n    assert Alias().price() == 2\n",
        encoding="utf-8",
    )
    (tmp_path / "irrelevant.py").write_text("def price(): pass\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("def price(): pass\n", encoding="utf-8")
    registry = ToolRegistry(tmp_path, PolicyEngine("read-only", workspace=tmp_path))
    register_navigation(registry)

    async def dispatch(mode: str, symbol: str, **params: str | int) -> ToolResult:
        return await registry.dispatch(
            ToolCall(mode, mode, json.dumps({"symbol": symbol, **params}))
        )

    signals = IssueSignals.extract(
        'File "/tmp/work/src/pkg/service.py", line 2, in price\nValueError: price()'
    )
    assert signals.frames[0][1] == 2 and signals.symbols == ("price",)
    assert signals.errors == ("ValueError",)
    definitions = await dispatch("find_symbol", "price", path="src/pkg/service.py", limit=1)
    assert definitions.data["matches"][0]["path"] == "src/pkg/service.py"
    assert definitions.data["truncated"]
    bindings = await dispatch("find_symbol", "pkg.C")
    assert bindings.data["matches"][0]["kind"] == "import_binding"
    relations = await dispatch("find_related_tests", "pkg.service.Calculator")
    assert relations.data["matches"][0]["path"] == "tests/test_service.py"
    refs = await dispatch("find_references", "pkg.service.Calculator")
    assert any(row["kind"] == "import_alias" for row in refs.data["matches"])
    target.write_text("\nclass Calculator:\n    def price(self): return 3\n", encoding="utf-8")
    refreshed = await dispatch("find_symbol", "pkg.service.Calculator.price")
    assert refreshed.data["matches"][0]["line"] == 3
    assert refreshed.data["matches"][0]["sha256"] != definitions.data["matches"][0]["sha256"]
    missing = await dispatch("find_symbol", "does_not_exist")
    assert missing.data["matches"] == [] and missing.data["coverage"] == "static_approximation"
    escaped = await dispatch("find_symbol", "price", path="../outside.py")
    assert escaped.data["code"] == "PATH_OUTSIDE_WORKSPACE"


@pytest.mark.parametrize(
    ("status", "data", "category"),
    [
        (
            "error",
            {"exit_code": 1, "stderr": "ModuleNotFoundError: fixture"},
            "IMPORT_OR_DEPENDENCY",
        ),
        ("error", {"exit_code": 1, "stderr": "SyntaxError: fixture"}, "SYNTAX_OR_RUNTIME"),
        ("error", {"code": "FILE_CHANGED"}, "PATCH_CONFLICT"),
        ("error", {"code": "RUNNER_IMAGE_UNAVAILABLE"}, "INFRASTRUCTURE_ERROR"),
        ("error", {"timed_out": True}, "TIMEOUT"),
        ("denied", {}, "SANDBOX_OR_POLICY_DENIED"),
        ("cancelled", {}, "CANCELLED"),
        ("unknown", {}, "UNKNOWN"),
    ],
)
def test_repair_classification(status, data, category) -> None:
    assert classify(ToolResult("call", "operation", "run_tests", status, "", data)) == category


async def test_enforced_repair_stops_repeated_failure_even_after_read(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_bytes(b"number = 1\n")
    calls = 0

    async def runner(argv: list[str], cwd: Path, **_kwargs: object) -> JSONDict:
        nonlocal calls
        calls += 1
        return {
            "status": "error",
            "data": {"exit_code": 1, "stderr": f"AssertionError: fixture {calls}"},
        }

    def reply(request: ModelRequest) -> AssistantReply:
        index = sum(message.role == "tool" for message in request.messages)
        return (
            action(index, "read_file", path="calc.py") if index % 2 else action(index, "run_tests")
        )

    config = AppConfig(harness=HarnessConfig(development_tools=True, repair_controller="enforce"))
    with SessionStore(tmp_path, tmp_path / "journal") as store:
        model = FakeModel(reply)
        run = build_agent(
            config,
            model,
            store,
            command_runner=runner,
            allow_sandbox_development=True,
            stream=False,
        )
        outcome = await run.run("Verify the fixture, preserving the workspace.")
        assert outcome.run_status == "failed" and calls == 3 and outcome.tool_calls == 5
        assert "without a changed candidate" in outcome.reason
        events = [
            event.payload for event in store.load_events() if event.type == "repair.diagnostic"
        ]
        assert events[-1]["category"] == "NO_PROGRESS" and events[-1]["repeat_count"] == 3
