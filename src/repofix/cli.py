"""Run RepoFix's locator, bounded repair, and experiment workflows."""

import argparse
import json
import re
import os
import sys
from pathlib import Path

from repofix.agents.engine import repair_runner
from repofix.agents.workflow import run_locator_task
from repofix.benchmarks.loader import load_task
from repofix.grader import grade_saved_patch
from repofix.execution.docker_runner import DEFAULT_IMAGE, DockerTestRunner
from repofix.execution.runner import run_test_command
from repofix.llm import OpenAICompatibleClient, ScriptedModelClient, ScriptedSequenceClient
from repofix.llm.env_file import read_env_file
from repofix.reporting import save_repair_run
from repofix.evaluation import audit_task, evaluate_submissions
from repofix.dashboard.server import serve_dashboard
from repofix.experiments import run_experiment
from repofix.experiment_compare import compare_evaluations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repofix")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command_name, help_text in (
        ("locate", "run the locator agent"),
        ("repair", "run locator, repair agent, and public-test validator"),
    ):
        command = subcommands.add_parser(command_name, help=help_text)
        command.add_argument("--task", type=Path, required=True)
        command.add_argument("--runner", choices=("host", "docker"), default="docker")
        command.add_argument("--image")
        command.add_argument(
            "--provider", choices=("mock", "api"), default="mock"
        )
        command.add_argument("--mock-response", type=Path)
        command.add_argument("--base-url")
        command.add_argument("--model")
        command.add_argument("--max-tokens", type=int)
        command.add_argument("--model-timeout", type=float)
        command.add_argument("--thinking", choices=("enabled", "disabled"))
        command.add_argument("--reasoning-effort", choices=("low", "high", "max"))
        command.add_argument(
            "--env-file",
            type=Path,
            help="local KEY=VALUE file (default: .env in the current directory)",
        )
        if command_name == "repair":
            command.add_argument("--workflow", choices=("two-agent", "single-agent", "competitive", "dynamic"), default="two-agent")
            command.add_argument("--engine", choices=("legacy", "langgraph"), default="legacy")
            command.add_argument("--context", choices=("ast-callgraph", "traceback-only"), default="ast-callgraph")
            command.add_argument("--mock-repair-response", type=Path)
            command.add_argument(
                "--output-root",
                type=Path,
                default=Path("runs"),
                help="directory for durable repair records (default: ./runs)",
            )
    grade = subcommands.add_parser(
        "grade", help="offline public and hidden acceptance of a saved patch"
    )
    grade.add_argument("--task", type=Path, required=True)
    grade.add_argument("--patch", type=Path, required=True)
    grade.add_argument("--runner", choices=("host", "docker"), default="docker")
    grade.add_argument("--image")
    check = subcommands.add_parser("check-task", help="audit one benchmark's buggy and reference versions")
    check.add_argument("--task", type=Path, required=True)
    check.add_argument("--runner", choices=("host", "docker"), default="docker")
    check.add_argument("--image")
    evaluate = subcommands.add_parser("evaluate", help="grade a fixed submission manifest")
    evaluate.add_argument("--manifest", type=Path, required=True)
    evaluate.add_argument("--submissions", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--runner", choices=("host", "docker"), default="docker")
    evaluate.add_argument("--image")
    experiment = subcommands.add_parser("experiment", help="compare repair workflows over ready tasks")
    experiment.add_argument("--manifest", type=Path, required=True)
    experiment.add_argument("--method", required=True)
    experiment.add_argument("--provider", choices=("mock", "api"), default="api")
    experiment.add_argument("--mock-response", type=Path)
    experiment.add_argument("--mock-repair-response", type=Path)
    experiment.add_argument("--base-url")
    experiment.add_argument("--model")
    experiment.add_argument("--env-file", type=Path)
    experiment.add_argument("--runner", choices=("host", "docker"), default="docker")
    experiment.add_argument("--image")
    experiment.add_argument("--limit", type=int)
    experiment.add_argument("--max-tokens", type=int, default=6000)
    experiment.add_argument("--model-timeout", type=float)
    experiment.add_argument("--thinking", choices=("enabled", "disabled"))
    experiment.add_argument("--reasoning-effort", choices=("low", "high", "max"))
    experiment.add_argument("--max-model-calls", type=int)
    experiment.add_argument(
        "--formal", action="store_true",
        help="preflight tasks and use bounded retries before offline grading",
    )
    experiment.add_argument(
        "--max-submission-attempts", type=int, default=3,
        help="formal mode: maximum repair attempts per task (default: 3)",
    )
    experiment.add_argument(
        "--task-id", action="append",
        help="run only this registered ready task (repeatable; partial experiment)",
    )
    experiment.add_argument("--workflow", choices=("two-agent", "single-agent", "competitive", "dynamic"), default="two-agent")
    experiment.add_argument("--engine", choices=("legacy", "langgraph"), default="legacy")
    experiment.add_argument("--context", choices=("ast-callgraph", "traceback-only"), default="ast-callgraph")
    experiment.add_argument("--output-root", type=Path, default=Path("runs/experiments"))
    compare = subcommands.add_parser("compare", help="compare completed full experiments without model calls")
    compare.add_argument("--evaluations", type=Path, nargs="+", required=True)
    compare.add_argument("--output", type=Path, required=True)
    dashboard = subcommands.add_parser("dashboard", help="serve the local repair and results dashboard")
    dashboard.add_argument(
        "--manifest", type=Path, action="append",
        help="benchmark manifest to display (repeatable)",
    )
    dashboard.add_argument("--runs", type=Path, default=Path("runs"))
    dashboard.add_argument("--evaluations", type=Path, default=Path("runs/evaluations"))
    dashboard.add_argument("--port", type=int, default=8765)
    return parser


def _create_client(args: argparse.Namespace):
    if args.provider == "mock":
        if args.mock_response is None:
            raise ValueError("--mock-response is required for the mock provider")
        return ScriptedModelClient(
            args.mock_response.read_text(encoding="utf-8")
        )

    file_settings = read_env_file(args.env_file or Path.cwd() / ".env")
    api_key = (
        os.environ.get("REPOFIX_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or file_settings.get("REPOFIX_API_KEY")
        or file_settings.get("DEEPSEEK_API_KEY")
    )
    if not api_key:
        raise ValueError(
            "set DEEPSEEK_API_KEY or REPOFIX_API_KEY in .env "
            "or an environment variable"
        )

    return OpenAICompatibleClient(
        api_key=api_key,
        base_url=(
            args.base_url
            or os.environ.get("REPOFIX_BASE_URL")
            or file_settings.get("REPOFIX_BASE_URL")
            or "https://api.deepseek.com"
        ),
        model=(
            args.model
            or os.environ.get("REPOFIX_MODEL")
            or file_settings.get("REPOFIX_MODEL")
            or "deepseek-v4-pro"
        ),
        max_tokens=(
            getattr(args, "max_tokens", None)
            or (3000 if getattr(args, "command", None) == "repair" else 1200)
        ),
        timeout_seconds=float(
            getattr(args, "model_timeout", None)
            or os.environ.get("REPOFIX_MODEL_TIMEOUT_SECONDS")
            or file_settings.get("REPOFIX_MODEL_TIMEOUT_SECONDS")
            or 180
        ),
        proxy_mode=(
            os.environ.get("REPOFIX_PROXY_MODE")
            or file_settings.get("REPOFIX_PROXY_MODE")
            or "system"
        ),
        thinking_mode=(
            getattr(args, "thinking", None)
            or os.environ.get("REPOFIX_THINKING")
            or file_settings.get("REPOFIX_THINKING")
        ),
        reasoning_effort=(
            getattr(args, "reasoning_effort", None)
            or os.environ.get("REPOFIX_REASONING_EFFORT")
            or file_settings.get("REPOFIX_REASONING_EFFORT")
        ),
    )


def _mock_repair_client(path: Path):
    """A JSON object repeats; a JSON array supplies sequential model answers."""
    raw = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ScriptedModelClient(raw)
    if isinstance(parsed, list):
        if not parsed or any(not isinstance(item, dict) for item in parsed):
            raise ValueError("mock repair sequence must be a nonempty array of objects")
        return ScriptedSequenceClient(tuple(
            json.dumps(item, ensure_ascii=False) for item in parsed
        ))
    return ScriptedModelClient(raw)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    if args.command == "dashboard":
        manifests = tuple(args.manifest or (Path("benchmarks/manifest.yaml"), Path("benchmarks/real/manifest.yaml")))
        try:
            serve_dashboard(
                manifests=manifests, runs_root=args.runs,
                evaluations_root=args.evaluations, port=args.port,
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(f"RepoFix error: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "evaluate":
        try:
            result = evaluate_submissions(
                args.manifest, args.submissions,
                backend=args.runner, image=args.image,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, RuntimeError, ValueError) as error:
            print(f"RepoFix error: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"output": str(args.output.resolve()), "methods": result["methods"]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "compare":
        try:
            result = compare_evaluations(tuple(args.evaluations))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            print(f"RepoFix error: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"output": str(args.output.resolve()), "methods": result["methods"]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "experiment":
        try:
            locator_client = _create_client(args)
            if args.provider == "mock" and args.workflow != "single-agent":
                if args.mock_repair_response is None:
                    raise ValueError("--mock-repair-response is required for mock experiment")
                repair_client = _mock_repair_client(args.mock_repair_response)
            else:
                repair_client = locator_client
            result = run_experiment(
                manifest_path=args.manifest, method=args.method,
                locator_client=locator_client, repair_client=repair_client,
                output_root=args.output_root, backend=args.runner,
                image=args.image, limit=args.limit, provider=args.provider,
                workflow_mode=args.workflow, context_mode=args.context,
                execution_engine=args.engine,
                task_ids=tuple(args.task_id) if args.task_id else None,
                max_model_calls=args.max_model_calls,
                formal=args.formal,
                max_submission_attempts=args.max_submission_attempts,
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(f"RepoFix error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    try:
        task = load_task(args.task)
    except (OSError, ValueError) as error:
        print(f"RepoFix error: {error}", file=sys.stderr)
        return 1
    if args.command == "check-task":
        try:
            result = audit_task(task, backend=args.runner, image=args.image)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"RepoFix error: {error}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 2
    test_runner = (
        DockerTestRunner(image=args.image or task.container_image or DEFAULT_IMAGE)
        if args.runner == "docker" else run_test_command
    )
    if args.command == "grade":
        try:
            grade = grade_saved_patch(
                task, args.patch, test_runner=test_runner
            )
        except (OSError, RuntimeError, ValueError) as error:
            print(f"RepoFix error: {error}", file=sys.stderr)
            return 1
        print(json.dumps({
            "task_id": grade.task_id,
            "status": grade.status,
            "public_test_status": (
                grade.public_test_status.value if grade.public_test_status else None
            ),
            "hidden_test_status": (
                grade.hidden_test_status.value if grade.hidden_test_status else None
            ),
            "reason": grade.reason,
            "patch_review": grade.review.to_dict() if grade.review else None,
        }, ensure_ascii=False, indent=2))
        return 0 if grade.passed else 2

    try:
        client = _create_client(args)
        if args.command == "repair":
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{1,90}", task.id)
                or task.id in {"experiments", "evaluations"}
            ):
                raise ValueError("task id cannot be used as a run directory name")
            if args.provider == "mock" and args.workflow != "single-agent":
                if args.mock_repair_response is None:
                    raise ValueError(
                        "--mock-repair-response is required for mock repair"
                    )
                repair_client = _mock_repair_client(args.mock_repair_response)
            else:
                repair_client = client
            repair_run = repair_runner(args.engine)(
                task, client, repair_client, test_runner=test_runner,
                workflow_mode=args.workflow, context_mode=args.context,
            )
            saved = save_repair_run(
                task=task,
                run=repair_run,
                output_root=args.output_root / task.id,
            )
        else:
            run = run_locator_task(task, client, test_runner=test_runner)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"RepoFix error: {error}", file=sys.stderr)
        return 1

    if args.command == "repair":
        validation = repair_run.validation_result
        repair = repair_run.repair_result
        candidate = repair.candidate
        hypothesis = repair_run.locator_result.hypothesis
        payload = {
            "task_id": repair_run.task_id,
            "execution_engine": repair_run.execution_engine,
            "initial_public_test_status": (
                repair_run.initial_public_test_status.value
            ),
            "locator_status": repair_run.locator_result.status,
            "target_file": (
                hypothesis.target_file.as_posix() if hypothesis else None
            ),
            "target_symbol": hypothesis.target_symbol if hypothesis else None,
            "repair_status": repair.status,
            "repair_explanation": repair.explanation,
            "candidate_explanation": (
                candidate.explanation if candidate else None
            ),
            "validation_status": validation.status if validation else None,
            "validation_reason": validation.reason if validation else None,
            "patch_review": validation.review.to_dict() if validation and validation.review else None,
            "public_tests_passed": (
                validation.public_tests_passed if validation else False
            ),
            "public_test_failures_after_patch": (
                len(validation.public_test_result.testfailures)
                if validation and validation.public_test_result
                else None
            ),
            "patch": validation.diff if validation else "",
            "run_directory": str(saved.run_directory),
            "report_path": str(saved.report_path),
            "markdown_report_path": str(saved.markdown_path) if saved.markdown_path else None,
            "patch_path": str(saved.patch_path) if saved.patch_path else None,
            "candidate_count": len(repair_run.attempts),
            "selected_attempt": repair_run.selected_attempt,
            "selection_reason": repair_run.selection_reason,
            "model_calls": repair_run.model_calls,
            "models": {
                "locator": repair_run.locator_result.model,
                "repair": repair.model,
            },
            "usage": {
                "locator_prompt_tokens": (
                    repair_run.locator_result.prompt_tokens
                ),
                "locator_completion_tokens": (
                    repair_run.locator_result.completion_tokens
                ),
                "repair_prompt_tokens": repair.prompt_tokens,
                "repair_completion_tokens": repair.completion_tokens,
                "total_prompt_tokens": repair_run.total_prompt_tokens,
                "total_completion_tokens": repair_run.total_completion_tokens,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if validation and validation.public_tests_passed else 2

    result = run.locator_result
    hypothesis = result.hypothesis
    payload = {
        "task_id": run.task_id,
        "public_test_status": run.public_test_status.value,
        "public_test_failures": run.public_test_failures,
        "locator_status": result.status,
        "hypothesis": (
            {
                "summary": hypothesis.summary,
                "root_cause": hypothesis.root_cause,
                "target_file": hypothesis.target_file.as_posix(),
                "target_symbol": hypothesis.target_symbol,
                "confidence": hypothesis.confidence,
                "evidence_ids": list(hypothesis.evidence_ids),
                "next_step": hypothesis.next_step,
            }
            if hypothesis is not None
            else None
        ),
        "explanation": result.explanation,
        "model": result.model,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "kind": item.kind,
                "file_path": (
                    item.file_path.as_posix()
                    if item.file_path is not None
                    else None
                ),
                "line_number": item.line_number,
                "symbol_name": item.symbol_name,
                "content": item.content,
            }
            for item in result.evidence
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
