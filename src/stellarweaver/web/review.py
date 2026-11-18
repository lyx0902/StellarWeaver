"""Candidate patches are generated once and selected by immutable hunk identifiers."""

import hashlib
import re
from pathlib import Path

from stellarweaver.domain import JSONDict
from stellarweaver.tools.workspace import ToolError, Workspace
from stellarweaver.web.isolation import git

_MAX_PATCH = 256 * 1024
_MAX_CHANGED = 100
_MAX_HUNKS = 300
_HUNK = re.compile(r"(?m)^@@ ")
_DIFF_OPTIONS = ("--no-renames", "--no-ext-diff", "--no-textconv", "--no-color")


def candidate(root: Path) -> JSONDict:
    workspace = Workspace(root)
    base = git(root, "rev-parse", "HEAD").decode().strip()
    new_paths = git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")[:-1]
    if len(new_paths) > _MAX_CHANGED:
        raise ToolError("PATCH_TOO_LARGE", "Too many files to build a reviewable patch.")
    if new_paths:
        for raw_path in new_paths:
            workspace.resolve(raw_path.decode())
        git(
            root,
            "add",
            "--intent-to-add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            patch=b"\0".join(new_paths) + b"\0",
        )
    changed = git(root, "diff", *_DIFF_OPTIONS, "--name-only", "-z", "HEAD", "--").split(b"\0")[:-1]
    if len(changed) > _MAX_CHANGED:
        raise ToolError("PATCH_TOO_LARGE", "Too many files to build a reviewable patch.")
    files = []
    size = 0
    hunk_count = 0
    for raw_path in changed:
        path = raw_path.decode()
        workspace.resolve(path, must_exist=False)
        raw = git(root, "diff", *_DIFF_OPTIONS, "--binary", "--full-index", "HEAD", "--", path)
        size += len(raw)
        if size > _MAX_PATCH:
            raise ToolError("PATCH_TOO_LARGE", "The reviewable patch exceeds 256 KiB.")
        try:
            body = raw.decode()
        except UnicodeDecodeError as exc:
            raise ToolError("PATCH_UNSUPPORTED", "Review requires UTF-8 text patches.") from exc
        split = [match.start() for match in _HUNK.finditer(body)]
        header = body[: split[0]] if split else body
        if "\0" in body or "\nGIT binary patch\n" in header or not split:
            raise ToolError("PATCH_UNSUPPORTED", "Review requires textual Git hunks.")
        hunks = []
        for start, end in zip(split, [*split[1:], len(body)], strict=True):
            hunk = body[start:end]
            digest = hashlib.sha256(f"{base}\0{path}\0{hunk}".encode()).hexdigest()
            hunks.append({"id": digest, "diff": hunk})
        hunk_count += len(hunks)
        if hunk_count > _MAX_HUNKS:
            raise ToolError("PATCH_TOO_LARGE", "The reviewable patch exceeds 300 hunks.")
        files.append({"path": path, "header": header, "hunks": hunks})
    return {"base": base, "files": files}


def selected_patch(review: JSONDict, identifiers: list[str]) -> bytes:
    selected = set(identifiers)
    seen: set[str] = set()
    parts = []
    for file in review["files"]:
        keep = [hunk for hunk in file["hunks"] if hunk["id"] in selected]
        if keep:
            parts.append(file["header"] + "".join(hunk["diff"] for hunk in keep))
            seen.update(hunk["id"] for hunk in keep)
    if not seen or seen != selected or len(selected) != len(identifiers):
        raise ToolError("INVALID_HUNKS", "Select valid, unique hunks from this report.")
    return "".join(parts).encode()


def apply_selection(root: Path, base: str, patch: bytes) -> None:
    if base != git(root, "rev-parse", "HEAD").decode().strip():
        raise ToolError("PATCH_BASE_CHANGED", "The patch and target commit differ.")
    try:
        # --recount retains correct counts when adjacent or earlier hunks are omitted.
        git(root, "apply", "--check", "--recount", "-", patch=patch)
        git(root, "apply", "--recount", "-", patch=patch)
    except ToolError as exc:
        raise ToolError(
            "PATCH_CONFLICT", "Git could not apply the selected hunks cleanly."
        ) from exc
