"""Helper CLI to pull official SWE-bench Lite images, build recipes.json, and prune images."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def official_image_name(instance_id: str) -> str:
    # Matches swebench 5.0.2 official Docker Hub naming convention.
    owner, rest = instance_id.split("__", 1)
    return f"swebench/sweb.eval.x86_64.{owner}_1776_{rest}:latest".lower()


def inspect_image(ref: str) -> tuple[str, str]:
    target = ref
    try:
        raw = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{json .}}", target],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.CalledProcessError:
        repo = ref.rsplit(":", 1)[0]
        found = subprocess.check_output(
            ["docker", "images", "--digests", "--format", "{{.Repository}}@{{.Digest}}", repo],
            text=True,
            encoding="utf-8",
            timeout=30,
        ).splitlines()
        digest_ref = next((line.strip() for line in found if "@sha256:" in line), "")
        if not digest_ref:
            raise ValueError(f"Local image not found for {ref}; run without --no-pull.") from None
        raw = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{json .}}", digest_ref],
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    info = json.loads(raw)
    image_id: str = info["Id"]
    digests: list[str] = info.get("RepoDigests") or []
    grader_image = next((d for d in digests if "@sha256:" in d), "")
    if not grader_image:
        raise ValueError(f"Image {ref} has no repo@sha256 digest; pull it from registry first.")
    return image_id, grader_image


def load_dataset(dataset: Path, grader_python: Path) -> list[dict[str, Any]]:
    official_script = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "stellarweaver"
        / "evals"
        / "swebench_official.py"
    )
    out = subprocess.check_output(
        [str(grader_python), "-I", str(official_script), "dataset", str(dataset)],
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    rows: list[dict[str, Any]] = json.loads(out)
    return rows


def default_pytest_args(row: dict[str, Any]) -> list[str]:
    # Extract visible test files from FAIL_TO_PASS / PASS_TO_PASS is forbidden for agent input!
    # Keep a generic, non-leaking repository test selector (or root pytest discovery).
    return ["-q", "--maxfail=3"]


def cmd_prepare_recipes(args: argparse.Namespace) -> int:
    rows = load_dataset(args.dataset.resolve(), args.grader_python.resolve())
    if args.repo:
        wanted_repos = set(args.repo)
        rows = [r for r in rows if r["repo"] in wanted_repos]
    if args.instance_id:
        wanted_ids = set(args.instance_id)
        rows = [r for r in rows if r["instance_id"] in wanted_ids]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No matching tasks found.", file=sys.stderr)
        return 2

    recipes: dict[str, dict[str, Any]] = {}
    if args.output.is_file() and args.merge:
        recipes = json.loads(args.output.read_text(encoding="utf-8"))

    for row in rows:
        key = row["instance_id"]
        ref = official_image_name(key)
        if not args.no_pull:
            for attempt in range(1, 5):
                print(f"Pulling {ref} (attempt {attempt}) ...", flush=True)
                try:
                    subprocess.run(["docker", "pull", ref], check=True, timeout=180)
                    break
                except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                    if attempt == 4:
                        raise
                    print(f"Pull stalled/failed for {ref}, retrying ...", flush=True)
        image_id, grader_image = inspect_image(ref)
        recipes[key] = {
            "image": image_id,
            "pytest_args": default_pytest_args(row),
            "grader_image": grader_image,
        }
        print(f"Prepared {key}: {image_id[:19]}...", flush=True)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(recipes, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(recipes)} recipe(s) to {args.output}")
    return 0


def _asset_is_valid(dest: Path, expected_commit: str) -> bool:
    if not (dest / ".git").exists():
        return False
    res = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return res.returncode == 0 and res.stdout.strip().lower() == expected_commit.lower()


def cmd_prefetch_assets(args: argparse.Namespace) -> int:
    cases_dir = args.experiment_dir / "cases"
    assets_dir: Path = args.assets
    assets_dir.mkdir(parents=True, exist_ok=True)
    proxy_args = ["-c", f"http.proxy={args.proxy}"] if args.proxy else []

    for case_dir in sorted(cases_dir.iterdir()):
        task_file = case_dir / "task.json"
        if not task_file.is_file():
            continue
        task = json.loads(task_file.read_text(encoding="utf-8"))
        instance_id: str = task["instance_id"]
        repo: str = task["repo"]
        base_commit: str = task["base_commit"]
        dest = assets_dir / instance_id

        if _asset_is_valid(dest, base_commit):
            print(f"Asset ready: {instance_id}", flush=True)
            continue

        for attempt in range(1, 6):
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True, exist_ok=True)
            print(f"Fetching {instance_id} ({repo}@{base_commit[:8]}, attempt {attempt}) ...", flush=True)
            try:
                subprocess.run(["git", "-C", str(dest), "init", "-q"], check=True, timeout=30)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(dest),
                        "-c",
                        "core.longpaths=true",
                        *proxy_args,
                        "fetch",
                        "--depth=1",
                        "--no-tags",
                        f"https://github.com/{repo}.git",
                        base_commit,
                    ],
                    check=True,
                    timeout=180,
                )
                subprocess.run(
                    ["git", "-C", str(dest), "-c", "core.longpaths=true", "checkout", "-q", "--detach", base_commit],
                    check=True,
                    timeout=60,
                )
                if _asset_is_valid(dest, base_commit):
                    print(f"Fetched {instance_id}", flush=True)
                    break
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                shutil.rmtree(dest, ignore_errors=True)
                if attempt == 5:
                    print(f"Failed to fetch {instance_id} after 5 attempts: {exc}", file=sys.stderr)
                    return 1
                print(f"Fetch failed for {instance_id} ({exc}), retrying in 3s ...", flush=True)
                time.sleep(3)
    return 0


def cmd_prune_images(args: argparse.Namespace) -> int:
    if args.report is not None:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        agg = report.get("aggregate", {})
        by_eval = agg.get("by_evaluation_status", {})
        not_eval = int(by_eval.get("not_evaluated", 0))
        eval_err = int(agg.get("evaluation_error", 0))
        if (not_eval > 0 or eval_err > 0) and not args.force:
            print(
                f"Refusing to prune: report has not_evaluated={not_eval}, evaluation_error={eval_err}.",
                file=sys.stderr,
            )
            return 3
    recipes = json.loads(args.recipes.read_text(encoding="utf-8"))
    removed = 0
    for key, entry in recipes.items():
        ref = entry.get("grader_image") or official_image_name(key)
        res = subprocess.run(["docker", "rmi", ref], capture_output=True, text=True)
        if res.returncode == 0:
            removed += 1
            print(f"Removed {ref}")
        else:
            print(f"Skipped {ref}: {res.stderr.strip()}")
    print(f"Pruned {removed} image(s).")
    return 0


def main() -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    subs = cli.add_subparsers(dest="action", required=True)

    prep = subs.add_parser("prepare-recipes", help="Pull official images and generate recipes.json")
    prep.add_argument("--dataset", type=Path, required=True)
    prep.add_argument("--grader-python", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--repo", action="append", help="Filter by owner/repo (repeatable)")
    prep.add_argument("--instance-id", action="append", help="Filter by instance_id (repeatable)")
    prep.add_argument("--limit", type=int)
    prep.add_argument("--no-pull", action="store_true", help="Inspect already-cached local images only")
    prep.add_argument("--merge", action="store_true", help="Merge into an existing recipes.json")

    fetch = subs.add_parser("prefetch-assets", help="Pre-fetch git base commits with retries")
    fetch.add_argument("--experiment-dir", type=Path, required=True)
    fetch.add_argument("--assets", type=Path, required=True)
    fetch.add_argument("--proxy", default="http://127.0.0.1:8003")

    prune = subs.add_parser("prune-images", help="Remove official SWE-bench images listed in recipes.json")
    prune.add_argument("--recipes", type=Path, required=True)
    prune.add_argument("--report", type=Path, help="Only prune if contract report has 0 not_evaluated/evaluation_error")
    prune.add_argument("--force", action="store_true")

    args = cli.parse_args()
    if args.action == "prepare-recipes":
        return cmd_prepare_recipes(args)
    if args.action == "prefetch-assets":
        return cmd_prefetch_assets(args)
    return cmd_prune_images(args)


if __name__ == "__main__":
    raise SystemExit(main())
