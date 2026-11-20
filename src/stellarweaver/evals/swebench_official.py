"""Run with the separate SWE-bench 5.0.2 environment, never the Agent environment."""

import argparse
import json
import os
import re
from importlib import import_module, metadata
from pathlib import Path
from typing import Any

GRADER_VERSION = "5.0.2"


def dataset_rows(source: Path) -> Any:
    if source.suffix == ".parquet":
        return import_module("pyarrow.parquet").read_table(source).to_pylist()
    return json.loads(source.read_text(encoding="utf-8"))


def log_directory(output: Path) -> Path:
    logs = output / "logs"
    if os.name != "nt":
        return logs
    # The upstream evaluator appends the run, model and case IDs to this path.
    text = str(logs)
    return Path("\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text)


def grade(case_file: Path, prediction_file: Path, output: Path, image: str, timeout: int) -> None:
    if metadata.version("swebench") != GRADER_VERSION:
        raise ValueError(f"Expected swebench=={GRADER_VERSION}")
    if not re.fullmatch(r"[a-z0-9/._:-]+@sha256:[a-f0-9]{64}", image, re.ASCII):
        raise ValueError("Official grading requires a repository image pinned by digest")
    if os.name == "nt":
        os.environ.setdefault("DOCKER_HOST", "npipe:////./pipe/dockerDesktopLinuxEngine")
    docker = import_module("docker")
    evaluation: Any = import_module("swebench.harness.run_evaluation")
    make_test_spec = import_module("swebench.harness.utils").make_test_spec
    case = json.loads(case_file.read_text(encoding="utf-8"))
    prediction = json.loads(prediction_file.read_text(encoding="utf-8"))
    if prediction["instance_id"] != case["instance_id"]:
        raise ValueError("Prediction and scorer case IDs differ")
    case["image"] = image
    spec = make_test_spec(case)
    original_create = evaluation.create_container
    original_copy = evaluation.copy_to_container
    original_log_directory = evaluation.RUN_EVALUATION_LOG_DIR
    client = docker.from_env(timeout=timeout + 60)

    def create(_spec: Any, container_client: Any, run_id: str, _logger: Any) -> Any:
        # A local get before creation prevents an implicit image pull.
        container_client.images.get(image)
        return container_client.containers.create(
            image=image,
            name=f"stellarweaver-swegrade-{run_id}",
            user="root",
            detach=True,
            command="tail -f /dev/null",
            network_mode="none",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=256,
            mem_limit="2g",
        )

    def copy(container: Any, source: Path, target: Path) -> Any:
        if target.name in {"eval.sh", "patch.diff"}:
            source.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
        return original_copy(container, source, target)

    try:
        evaluation.create_container = create
        evaluation.copy_to_container = copy
        evaluation.RUN_EVALUATION_LOG_DIR = log_directory(output)
        result = evaluation.run_instance(spec, prediction, client, output.name, timeout=timeout)
        if result is None:
            raise RuntimeError("SWE-bench did not produce a report. See the attempt logs.")
        (output / "report.json").write_text(json.dumps(result[1], indent=2), encoding="utf-8")
    finally:
        evaluation.create_container = original_create
        evaluation.copy_to_container = original_copy
        evaluation.RUN_EVALUATION_LOG_DIR = original_log_directory
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    dataset = subcommands.add_parser("dataset")
    dataset.add_argument("source", type=Path)
    evaluator = subcommands.add_parser("evaluate")
    evaluator.add_argument("case", type=Path)
    evaluator.add_argument("prediction", type=Path)
    evaluator.add_argument("output", type=Path)
    evaluator.add_argument("image")
    evaluator.add_argument("timeout", type=int)
    args = parser.parse_args()
    if args.action == "dataset":
        print(json.dumps(dataset_rows(args.source), ensure_ascii=True))
    else:
        grade(
            args.case.resolve(),
            args.prediction.resolve(),
            args.output.resolve(),
            args.image,
            args.timeout,
        )


if __name__ == "__main__":
    main()
