"""Explicit offline/live evaluation and independent rescoring entry point."""

import argparse
import asyncio
import json
from pathlib import Path

from stellarweaver.evals import catalog, run_suite, score_report
from stellarweaver.evals.provenance import write_receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Versioned 20-task StellarWeaver suite; fake measures infrastructure only."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List fixed task IDs and categories.")
    run = commands.add_parser(
        "run", help="Materialize fresh Git fixtures, run the agent and independent oracle."
    )
    run.add_argument("--provider", choices=("fake", "deepseek"), default="fake")
    run.add_argument("--task", action="append", dest="tasks")
    run.add_argument("--output", type=Path, help="New output directory, must not already exist.")
    run.add_argument("--env-file", type=Path)
    score = commands.add_parser(
        "score", help="Re-evaluate saved workspaces without calling a model."
    )
    score.add_argument("report", type=Path)
    score.add_argument("--task", action="append", dest="tasks")
    score.add_argument(
        "--catalog",
        type=Path,
        help="Explicit trusted historical scoring catalog; never imports archived Python code.",
    )
    score.add_argument(
        "--output",
        type=Path,
        help="Create a new receipt and hash-addressed scoring source bundle; never overwrite.",
    )
    args = parser.parse_args()
    try:
        if args.command == "list":
            print(
                json.dumps(
                    [
                        {key: task[key] for key in ("task_id", "category", "title")}
                        for task in catalog()
                    ],
                    ensure_ascii=True,
                    indent=2,
                )
            )
        elif args.command == "score":
            if args.output is not None and (
                args.output.exists()
                or args.output.is_symlink()
                or args.output.with_name(args.output.name + ".provenance").exists()
            ):
                raise FileExistsError("Receipt or source bundle already exists")
            result = score_report(args.report, args.tasks, catalog_path=args.catalog)
            if args.output is not None:
                write_receipt(result, args.output, args.catalog)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result["passed"] == result["attempted"] else 1
        else:
            path = asyncio.run(
                run_suite(
                    provider=args.provider,
                    task_ids=args.tasks,
                    output=args.output,
                    env_file=args.env_file,
                )
            )
            report = json.loads(path.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    {"report": str(path), "metrics": report["metrics"]}, ensure_ascii=True, indent=2
                )
            )
            return 0 if report["metrics"]["failed"] == 0 else 1
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"Evaluation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
