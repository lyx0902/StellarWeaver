"""Build real archives from a synthetic checkout with local-only material."""

import json
import os
import subprocess
import tarfile
import zipfile
from pathlib import Path


def test_source_and_wheel_exclude_local_material_and_keep_runtime_resources(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source_root = Path(__file__).parents[2]
    (project / "pyproject.toml").write_bytes((source_root / "pyproject.toml").read_bytes())
    # VCS-independent selection is important: the developer checkout has no .git.
    files = {
        "README.md": "# Synthetic package\n",
        "src/stellarweaver/__init__.py": '__version__ = "0.1.0"\n',
        "src/stellarweaver/cli.py": "def app(): pass\n",
        "src/stellarweaver/evals/catalog.json": json.dumps([{"task_id": "B01"}]),
        "examples/mcp_server.py": "# local example\n",
        "evals/fixtures/B01/target.py": "value = 1\n",
        "docs/ARCHITECTURE.md": "# Architecture\n",
        ".env.example": "STELLARWEAVER_API_KEY=\n",
        ".env": "LOCAL_ONLY_TEST_CREDENTIAL=not-a-real-key\n",
        ".idea/workspace.xml": "LOCAL_ONLY_IDE_STATE",
        ".codex/session.json": "LOCAL_ONLY_CODEX_STATE",
        "docs/validation/session_report.json": "LOCAL_ONLY_MACHINE_REPORT",
        "evals/runs/local/report.json": "LOCAL_ONLY_EVAL_RUN",
        "docs/notes/.env": "LOCAL_ONLY_NESTED_CREDENTIAL",
        "docs/private.pem": "LOCAL_ONLY_PRIVATE_KEY",
        "src/stellarweaver/accidental.sqlite3": "LOCAL_ONLY_DATABASE",
    }
    for name, text in files.items():
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    result = subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--project",
            str(project),
            "--out-dir",
            str(tmp_path / "dist"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source = next((tmp_path / "dist").glob("*.tar.gz"))
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with tarfile.open(source) as archive:
        names = [member.name.split("/", 1)[1] for member in archive.getmembers() if member.isfile()]
        assert ".env.example" in names
        assert "src/stellarweaver/evals/catalog.json" in names
        assert "examples/mcp_server.py" in names
        assert "evals/fixtures/B01/target.py" in names
        assert "docs/ARCHITECTURE.md" in names
        for member in archive.getmembers():
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                assert b"LOCAL_ONLY_" not in stream.read(), member.name
        assert not any(
            ".idea" in name or "docs/validation" in name or ".codex" in name for name in names
        )
    direct = subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--project",
            str(project),
            "--out-dir",
            str(tmp_path / "direct-wheel"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert direct.returncode == 0, direct.stdout + direct.stderr
    # A direct wheel build must be safe too; building via the filtered sdist alone
    # would hide an overly permissive wheel manifest.
    for candidate in [wheel, *(tmp_path / "direct-wheel").glob("*.whl")]:
        with zipfile.ZipFile(candidate) as archive:
            assert "stellarweaver/evals/catalog.json" in archive.namelist()
            for name in archive.namelist():
                assert b"LOCAL_ONLY_" not in archive.read(name), name
