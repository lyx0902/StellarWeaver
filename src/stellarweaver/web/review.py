"""Candidate patches are generated once and selected by immutable hunk identifiers."""

import hashlib
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from stellarweaver.domain import JSONDict
from stellarweaver.tools.workspace import ToolError, Workspace
from stellarweaver.web.isolation import git, task_worktree

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


def _selected_files(review: JSONDict, identifiers: list[str]) -> list[tuple[JSONDict, str]]:
    selected = set(identifiers)
    seen: set[str] = set()
    parts: list[tuple[JSONDict, str]] = []
    for file in review["files"]:
        keep = [hunk for hunk in file["hunks"] if hunk["id"] in selected]
        if keep:
            parts.append((file, file["header"] + "".join(hunk["diff"] for hunk in keep)))
            seen.update(hunk["id"] for hunk in keep)
    if not seen or seen != selected or len(selected) != len(identifiers):
        raise ToolError("INVALID_HUNKS", "Select valid, unique hunks from this report.")
    return parts


def selected_patch(review: JSONDict, identifiers: list[str]) -> bytes:
    return "".join(diff for _, diff in _selected_files(review, identifiers)).encode()


def feedback_prompt(
    review: JSONDict, run: str, path: str, hunk_id: str | None, feedback: str
) -> str:
    file = next((file for file in review["files"] if file["path"] == path), None)
    if file is None:
        raise ToolError("INVALID_REVIEW_TARGET", "反馈文件不在本轮候选补丁中。")
    anchor = {"run_id": run, "base": review["base"], "path": file["path"]}
    if hunk_id is not None:
        hunk = next((hunk for hunk in file["hunks"] if hunk["id"] == hunk_id), None)
        if hunk is None:
            raise ToolError("INVALID_REVIEW_TARGET", "反馈改动块不属于所选文件。")
        anchor.update(hunk=hunk_id, range=hunk["diff"].splitlines()[0][:160])
    if not feedback.strip():
        raise ToolError("EMPTY_FEEDBACK", "请输入审阅意见。")
    location = json.dumps(anchor, ensure_ascii=False)
    prompt = f"请在原任务工作区按以下审阅意见修订。先查看当前文件和差异。\n审阅位置：{location}\n\n{feedback.strip()}"
    if len(prompt) > 8000:
        raise ToolError("FEEDBACK_TOO_LARGE", "审阅位置和意见超出任务输入上限。")
    return prompt


def _checkout(root: Path) -> tuple[str, str]:
    if git(root, "rev-parse", "--show-prefix").strip():
        raise ToolError("INVALID_TARGET", "预检目标必须是 Git 仓库根目录。")
    return (
        git(root, "rev-parse", "HEAD").decode().strip(),
        git(root, "rev-parse", "--abbrev-ref", "HEAD").decode().strip(),
    )


def _entries(output: bytes) -> dict[str, list[list[bytes]]]:
    entries: dict[str, list[list[bytes]]] = {}
    for row in output.split(b"\0")[:-1]:
        metadata, path = row.split(b"\t", 1)
        entries.setdefault(path.decode(), []).append(metadata.split())
    return entries


def _local_changes(root: Path, clean: Path, paths: list[str]) -> list[str]:
    # Inspect the original index with plumbing. The clean checkout uses its own
    # Git configuration, so the target's custom filters are never needed.
    index = _entries(git(root, "ls-files", "--stage", "-z", "--", *paths))
    head = _entries(git(clean, "ls-files", "--stage", "-z", "--", *paths))
    attributes = git(clean, "check-attr", "--cached", "-z", "text", "--", *paths).split(b"\0")
    text_mode = dict(zip(attributes[0:-1:3], attributes[2::3], strict=True))
    changed: list[str] = []
    for name in paths:
        actual = root / name
        baseline = clean / name
        if index.get(name) != head.get(name) or any(
            parent.is_symlink() or (parent.exists() and not parent.is_dir())
            for parent in actual.parents
            if parent != root and parent.is_relative_to(root)
        ):
            changed.append(name)
        elif actual.is_symlink() or (actual.exists() and not actual.is_file()):
            changed.append(name)
        elif baseline.is_file() and actual.is_file():
            expected = baseline.read_bytes()
            # The host checkout can have CRLF while the isolated Git checkout uses
            # LF. Only allow that conversion for text; keep explicit -text exact.
            normalize = text_mode.get(name.encode()) != b"unset" and b"\0" not in expected
            canonical = expected.replace(b"\r\n", b"\n") if normalize else expected
            with actual.open("rb") as stream:
                limit = len(canonical) + canonical.count(b"\n") if normalize else len(expected)
                content = stream.read(limit + 1)
                if normalize:
                    content = content.replace(b"\r\n", b"\n")
                if content != canonical:
                    changed.append(name)
        elif baseline.is_file() or actual.exists():
            changed.append(name)
    return changed


def _patch_applies(root: Path, patch: bytes) -> bool:
    try:
        git(root, "apply", "--check", "--recount", "-", patch=patch)
    except ToolError:
        return False
    return True


def preflight_patch(
    target: Path, tenant: str, run: str, review: JSONDict, identifiers: list[str]
) -> JSONDict:
    parts = [(file["path"], diff) for file, diff in _selected_files(review, identifiers)]
    paths = [path for path, _ in parts]
    patch = "".join(diff for _, diff in parts).encode()
    head, branch = _checkout(target)
    with TemporaryDirectory(prefix="sw-preflight-") as directory:
        clean = task_worktree(Path(directory).resolve(), tenant, run, target)
        if git(clean, "rev-parse", "HEAD").decode().strip() != head:
            raise ToolError("TARGET_CHANGED", "目标分支已变化，请重新预检。")
        applies = _patch_applies(clean, patch)
        conflicts = (
            []
            if applies
            else [path for path, diff in parts if not _patch_applies(clean, diff.encode())]
        )
        dirty = _local_changes(target, clean, paths)
        if _checkout(target) != (head, branch):
            raise ToolError("TARGET_CHANGED", "目标分支已变化，请重新预检。")
    base = review["base"]
    changed_since_base = None
    if base != head:
        try:
            changed_since_base = (
                git(target, "diff", "--name-only", "-z", *_DIFF_OPTIONS, base, head, "--", *paths)
                .decode()
                .split("\0")[:-1]
            )
        except ToolError:
            pass  # The original base may no longer be present in the target repository.
    status = "ready"
    if not applies:
        status = "conflict"
    elif dirty:
        status = "local_changes"
    elif base != head:
        status = "base_changed"
    return {
        "status": status,
        "branch": None if branch == "HEAD" else branch,
        "target_head": head,
        "source_base": base,
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "paths": paths,
        "conflict_paths": conflicts if conflicts or applies else paths,
        "local_changes": dirty,
        "changed_since_base": changed_since_base,
        "entire_selection": len(identifiers) == sum(len(file["hunks"]) for file in review["files"]),
    }


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
