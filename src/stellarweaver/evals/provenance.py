"""Content-addressed scoring sources and exclusively created audit receipts.

Hashes identify the rules actually used for this invocation. Source snapshots
are data: this module never imports or executes an archived Python source file.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from stellarweaver import __version__

SCORING_VERSION = "stellarweaver-evals/1"
_SOURCE_NAMES = ("__init__.py", "__main__.py", "provenance.py")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sources(catalog_path: Path | None = None) -> dict[str, bytes]:
    package = Path(__file__).parent
    selected = catalog_path or package / "catalog.json"
    result = {"catalog.json": selected.read_bytes()}
    result.update({name: (package / name).read_bytes() for name in _SOURCE_NAMES})
    return result


def identity(catalog_path: Path | None = None) -> dict[str, Any]:
    blobs = sources(catalog_path)
    files = {name: digest(raw) for name, raw in blobs.items()}
    code = {name: value for name, value in files.items() if name.endswith(".py")}
    code_hash = digest(json.dumps(code, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {
        "scoring_version": SCORING_VERSION,
        "package_version": __version__,
        "catalog_sha256": files["catalog.json"],
        "scorer_sha256": code_hash,
        "files_sha256": files,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


def archive(directory: Path, expected: Mapping[str, Any], catalog_path: Path | None = None) -> None:
    """Create a new bundle; refuse source drift or an existing target."""
    blobs = sources(catalog_path)
    actual = {name: digest(raw) for name, raw in blobs.items()}
    if actual != expected["files_sha256"]:
        raise ValueError("Scoring source changed during grading; refusing an inconsistent receipt")
    directory.mkdir(parents=False, exist_ok=False)
    for name, raw in blobs.items():
        asset = directory / f"{actual[name]}-{name}"
        with asset.open("xb") as stream:
            stream.write(raw)
    with (directory / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(dict(expected), stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def verify(directory: Path, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Verify bytes and identities without executing archived scoring code."""
    with (directory / "manifest.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files_sha256"), dict):
        raise ValueError("Invalid scoring provenance manifest")
    if expected is not None and dict(expected) != manifest:
        raise ValueError("Scoring manifest differs from the receipt")
    if set(manifest["files_sha256"]) != {"catalog.json", *_SOURCE_NAMES}:
        raise ValueError("Unexpected scoring source set")
    for name, checksum in manifest["files_sha256"].items():
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
        ):
            raise ValueError("Invalid scoring source checksum")
        asset = directory / f"{checksum}-{name}"
        if asset.is_symlink() or digest(asset.read_bytes()) != checksum:
            raise ValueError(f"Scoring source hash mismatch: {name}")
    code = {name: value for name, value in manifest["files_sha256"].items() if name.endswith(".py")}
    if digest(json.dumps(code, sort_keys=True, separators=(",", ":")).encode()) != manifest.get(
        "scorer_sha256"
    ):
        raise ValueError("Scorer aggregate hash mismatch")
    if manifest["files_sha256"]["catalog.json"] != manifest.get("catalog_sha256"):
        raise ValueError("Catalog hash mismatch")
    return manifest


def write_receipt(
    result: Mapping[str, Any], output: Path, catalog_path: Path | None = None
) -> None:
    """Write a new JSON receipt and a sibling source bundle, without overwrite."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Receipt already exists: {output}")
    provenance = result["provenance"]
    directory = output.with_name(output.name + ".provenance")
    archive(directory, provenance, catalog_path)
    public = {**result, "provenance_bundle": directory.name}
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(public, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
