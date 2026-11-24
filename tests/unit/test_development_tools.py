"""Unit tests for fine-grained run_tests filtering and run_python_snippet."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from stellarweaver.config import AppConfig, HarnessConfig
from stellarweaver.domain import ToolCall
from stellarweaver.policy import DEVELOPMENT_TOOLS, PolicyEngine
from stellarweaver.providers.fake import FakeModel
from stellarweaver.runtime.service import build_agent
from stellarweaver.session.store import SessionStore
from stellarweaver.tools.commands import execute_command
from stellarweaver.tools.development import (
    DevelopmentArgs,
    SnippetArgs,
    decode_development,
    development_argv,
    register_development_tools,
)
from stellarweaver.tools.registry import ToolRegistry


def test_development_argv_and_decode_roundtrip_selector_keyword_and_snippet() -> None:
    args = DevelopmentArgs(
        target="tests/test_sample.py",
        framework="pytest",
        selector="TestGroup::test_case",
        keyword="case and not slow",
    )
    argv = development_argv("run_tests", args)
    decoded = decode_development(argv)
    assert decoded is not None
    action, parsed = decoded
    assert action == "run_tests"
    assert isinstance(parsed, DevelopmentArgs)
    assert parsed == args

    snippet = SnippetArgs(code="print(1 + 1)")
    s_argv = development_argv("run_python_snippet", snippet)
    s_decoded = decode_development(s_argv)
    assert s_decoded is not None
    s_action, s_parsed = s_decoded
    assert s_action == "run_python_snippet"
    assert isinstance(s_parsed, SnippetArgs)
    assert s_parsed.code == "print(1 + 1)"


@pytest.mark.parametrize("selector", ["--exec", "a/b", "test_a; rm -rf /", "Test::"])
def test_development_args_rejects_unsafe_selector(selector: str) -> None:
    with pytest.raises(ValueError):
        DevelopmentArgs(target="tests/test_a.py", framework="pytest", selector=selector)


@pytest.mark.parametrize("keyword", ["--help", "a; b", "x && y", "   "])
def test_development_args_rejects_unsafe_keyword(keyword: str) -> None:
    with pytest.raises(ValueError):
        DevelopmentArgs(target="tests/test_a.py", framework="pytest", keyword=keyword)


@pytest.mark.parametrize("code", ["   ", "print('a\x00b')"])
def test_snippet_args_rejects_invalid_code(code: str) -> None:
    with pytest.raises(ValueError):
        SnippetArgs(code=code)


async def test_run_tests_selector_keyword_and_snippet_execute_in_runner(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n"
        "def test_alpha():\n    assert add(1, 2) == 3\n"
        "def test_beta():\n    assert False\n",
        encoding="utf-8",
    )

    async def local_runner(argv, cwd, *, config=None, artifacts_dir=None, timeout_seconds=None):
        patched = [sys.executable, *argv[1:]] if argv and argv[0] == "python" else argv
        return await execute_command(
            patched, cwd, config=config, artifacts_dir=artifacts_dir, timeout_seconds=timeout_seconds
        )

    engine = PolicyEngine("auto", workspace=tmp_path, allow_sandbox_development=True)
    registry = ToolRegistry(
        tmp_path,
        engine,
        command_runner=local_runner,
        command_boundary="test-boundary",
    )
    register_development_tools(registry)

    filtered = await registry.dispatch(
        ToolCall(
            "c1",
            "run_tests",
            json.dumps(
                {
                    "target": "test_calc.py",
                    "framework": "pytest",
                    "selector": "test_alpha",
                }
            ),
        )
    )
    assert filtered.status == "ok"
    assert filtered.data["exit_code"] == 0
    assert filtered.data["diagnostic"]["selector"] == "test_alpha"

    kw = await registry.dispatch(
        ToolCall(
            "c2",
            "run_tests",
            json.dumps({"target": "test_calc.py", "framework": "pytest", "keyword": "alpha"}),
        )
    )
    assert kw.status == "ok"
    assert kw.data["exit_code"] == 0

    snippet = await registry.dispatch(
        ToolCall(
            "c3",
            "run_python_snippet",
            json.dumps({"code": "from calc import add\nprint('SUM=', add(20, 22))"}),
        )
    )
    assert snippet.status == "ok"
    assert snippet.data["exit_code"] == 0
    assert "SUM= 42" in snippet.content


def test_benchmark_policy_exposes_git_history_and_run_python_snippet(tmp_path: Path) -> None:
    async def runner(argv, cwd, *, config=None, artifacts_dir=None, timeout_seconds=None):
        return {"status": "ok", "content": "", "data": {"exit_code": 0}}

    config = AppConfig(
        policy={"preset": "benchmark"},
        harness=HarnessConfig(development_tools=True),
    )
    with SessionStore(tmp_path, tmp_path / "state") as store:
        agent = build_agent(
            config,
            FakeModel([]),
            store,
            command_runner=runner,
            command_boundary="bench",
            allow_sandbox_development=True,
        )
        names = {schema["function"]["name"] for schema in agent.registry.schemas()}
    assert {"git_log", "git_show", "run_python_snippet", "run_tests"} <= names
    assert "run_python_snippet" in DEVELOPMENT_TOOLS
