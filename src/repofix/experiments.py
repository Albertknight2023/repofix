"""Run the existing two-Agent repair workflow over a fixed benchmark manifest."""

import json
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from repofix.agents.engine import repair_runner
from repofix.benchmarks.loader import load_tasks
from repofix.benchmarks.models import TaskSpec
from repofix.evaluation import audit_task, evaluate_submissions, task_runner
from repofix.llm import ChatMessage, ModelClient, ModelResponse
from repofix.llm.client import IncompleteModelResponseError
from repofix.reporting import save_repair_run


def _fingerprint_files(files: list[tuple[str, Path]]) -> str:
    """Hash names and contents, excluding generated caches from experiment identity."""

    digest = hashlib.sha256()
    for name, path in sorted(files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _experiment_fingerprints(
    manifest_path: Path, tasks: tuple[TaskSpec, ...]
) -> tuple[str, str]:
    source_root = Path(__file__).resolve().parent
    source_files = [
        (path.relative_to(source_root).as_posix(), path)
        for path in source_root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and (path.suffix == ".py" or (
            path.suffix == ".md" and "prompts" in path.parts
        ))
    ]
    task_files = [("manifest.yaml", manifest_path.resolve())]
    for task in tasks:
        task_files.extend(
            (f"{task.id}/{path.relative_to(task.task_root).as_posix()}", path)
            for path in task.task_root.rglob("*")
            if path.is_file()
            and not {"__pycache__", ".pytest_cache", ".git"}.intersection(path.parts)
            and path.suffix != ".pyc"
        )
    return _fingerprint_files(source_files), _fingerprint_files(task_files)


@dataclass(slots=True)
class _CallBudget:
    limit: int | None
    used: int = 0

    def reserve(self) -> None:
        if self.limit is not None and self.used >= self.limit:
            raise RuntimeError("experiment model-call budget exhausted")
        self.used += 1


@dataclass(slots=True)
class _TrackedClient:
    """Count model attempts without retaining prompts, responses, or credentials."""

    client: ModelClient
    budget: _CallBudget
    attempted: int = 0
    succeeded: int = 0
    prompt_tokens_observed: int = 0
    completion_tokens_observed: int = 0
    reasoning_tokens_observed: int = 0
    calls_with_unknown_usage: int = 0

    def complete(self, messages: tuple[ChatMessage, ...]) -> ModelResponse:
        self.budget.reserve()
        self.attempted += 1
        try:
            response = self.client.complete(messages)
        except (OSError, RuntimeError, ValueError) as error:
            prompt = getattr(error, "prompt_tokens", None)
            completion = getattr(error, "completion_tokens", None)
            reasoning = getattr(error, "reasoning_tokens", None)
            if prompt is None or completion is None:
                self.calls_with_unknown_usage += 1
            self.prompt_tokens_observed += prompt or 0
            self.completion_tokens_observed += completion or 0
            self.reasoning_tokens_observed += reasoning or 0
            raise
        self.succeeded += 1
        if response.prompt_tokens is None or response.completion_tokens is None:
            self.calls_with_unknown_usage += 1
        self.prompt_tokens_observed += response.prompt_tokens or 0
        self.completion_tokens_observed += response.completion_tokens or 0
        self.reasoning_tokens_observed += response.reasoning_tokens or 0
        return response


def run_experiment(
    *, manifest_path: Path, method: str,
    locator_client: ModelClient, repair_client: ModelClient,
    output_root: Path, backend: str = "docker", image: str | None = None,
    limit: int | None = None,
    provider: str = "api",
    workflow_mode: str = "two-agent",
    context_mode: str = "ast-callgraph",
    execution_engine: str = "legacy",
    task_ids: tuple[str, ...] | None = None,
    max_model_calls: int | None = None,
    formal: bool = False,
    max_submission_attempts: int = 3,
) -> dict[str, object]:
    """Generate saved patches first, then grade them without involving Agents."""

    if not method or any(character in method for character in "\\/\r\n"):
        raise ValueError("method must be a simple non-empty label")
    if workflow_mode not in {"two-agent", "single-agent", "competitive", "dynamic"}:
        raise ValueError("invalid workflow_mode")
    if context_mode not in {"ast-callgraph", "traceback-only"}:
        raise ValueError("context_mode must be ast-callgraph or traceback-only")
    runner_function = repair_runner(execution_engine)
    if max_model_calls is not None and max_model_calls < 1:
        raise ValueError("max_model_calls must be positive")
    if not 1 <= max_submission_attempts <= 10:
        raise ValueError("max_submission_attempts must be between 1 and 10")
    if formal and provider == "api" and max_model_calls is None:
        raise ValueError("formal API experiments require --max-model-calls")
    tasks = load_tasks(manifest_path)
    implementation_fingerprint, dataset_fingerprint = _experiment_fingerprints(
        manifest_path, tasks
    )
    if task_ids is not None:
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise ValueError("task_ids must be nonempty and unique")
        registered = {task.id for task in tasks}
        unknown = set(task_ids) - registered
        if unknown:
            raise ValueError(f"task_ids not ready in manifest: {', '.join(sorted(unknown))}")
        selected = set(task_ids)
        tasks = tuple(task for task in tasks if task.id in selected)
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        tasks = tasks[:limit]
    if not tasks:
        raise ValueError("manifest has no selected ready tasks")

    preflight: list[dict[str, object]] = []
    if formal:
        for task in tasks:
            audit = audit_task(task, backend=backend, image=image)
            preflight.append(audit)
            if not audit["ready"]:
                raise ValueError(
                    f"formal preflight failed for {task.id}: {audit['reason']} "
                    f"(initial={audit['initial_status']}, reference={audit['reference_status']})"
                )

    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    directory = output_root.resolve() / run_id
    directory.mkdir(parents=True, exist_ok=False)
    submissions: list[dict[str, str]] = []
    task_errors: list[dict[str, str]] = []
    retry_records: list[dict[str, object]] = []
    usage_records: list[dict[str, object]] = []
    budget = _CallBudget(max_model_calls)
    for task in tasks:
        entry = {"method": method, "task_id": task.id}
        tracked_locator = _TrackedClient(locator_client, budget)
        tracked_repair = (
            tracked_locator if repair_client is locator_client
            else _TrackedClient(repair_client, budget)
        )
        started = perf_counter()
        repair = None
        completed = False
        feedback: str | None = None
        attempts_used = 0
        terminal_infrastructure_error: str | None = None
        for attempt_number in range(1, max_submission_attempts + 1 if formal else 2):
            attempts_used = attempt_number
            try:
                runner = task_runner(task, backend, image)
                repair = runner_function(
                    task, tracked_locator, tracked_repair, test_runner=runner,
                    workflow_mode=workflow_mode, context_mode=context_mode,
                    retry_feedback=feedback,
                )
                terminal_infrastructure_error = None
                saved = save_repair_run(
                    task=task, run=repair, output_root=directory / "runs"
                )
                validation = repair.validation_result
                admissible = (
                    saved.patch_path is not None
                    and validation is not None
                    and validation.status != "rejected"
                    and bool(validation.diff)
                )
                if admissible:
                    entry["patch"] = str(saved.patch_path)
                    completed = True
                    break
                feedback = (
                    validation.reason if validation is not None
                    else repair.repair_result.explanation
                )[:500]
                entry["no_submission_reason"] = feedback
                retry_records.append({
                    "task_id": task.id, "attempt": attempt_number,
                    "kind": "invalid_candidate", "reason": feedback,
                })
                completed = True
                if not formal:
                    break
            except (OSError, RuntimeError, ValueError) as error:
                reason = str(error)[:300]
                retry_records.append({
                    "task_id": task.id, "attempt": attempt_number,
                    "kind": "workflow_exception", "error_type": type(error).__name__,
                    "reason": reason,
                })
                if not formal:
                    task_errors.append({
                        "task_id": task.id,
                        "error_type": type(error).__name__, "error": reason,
                    })
                    break
                feedback = f"Previous submission failed: {reason}"[:500]
                infrastructure = (
                    isinstance(error, (OSError, RuntimeError, IncompleteModelResponseError))
                    or reason.startswith("model API")
                )
                terminal_infrastructure_error = reason if infrastructure else None
                entry["no_submission_reason"] = reason
                if reason == "experiment model-call budget exhausted":
                    break
        if formal and terminal_infrastructure_error and "patch" not in entry:
            task_errors.append({
                "task_id": task.id, "error_type": "InfrastructureError",
                "error": terminal_infrastructure_error,
            })
        trackers = (
            (tracked_locator,) if tracked_repair is tracked_locator
            else (tracked_locator, tracked_repair)
        )
        usage_records.append({
            "task_id": task.id,
            "status": (
                "submitted" if formal and "patch" in entry else
                "infrastructure_error" if formal and terminal_infrastructure_error else
                "no_submission" if formal else
                "completed" if completed else "workflow_error"
            ),
            "submission_attempts": attempts_used,
            "model_calls": sum(item.attempted for item in trackers),
            "model_calls_succeeded": sum(item.succeeded for item in trackers),
            "prompt_tokens": sum(item.prompt_tokens_observed for item in trackers),
            "completion_tokens": sum(item.completion_tokens_observed for item in trackers),
            "reasoning_tokens_observed": sum(
                item.reasoning_tokens_observed for item in trackers
            ),
            "calls_with_unknown_usage": sum(
                item.calls_with_unknown_usage for item in trackers
            ),
            "candidate_count": len(repair.attempts) if repair else 0,
            "selected_attempt": repair.selected_attempt if repair else None,
            "repair_seconds": round(perf_counter() - started, 3),
        })
        submissions.append(entry)

    submission_path = directory / "submissions.json"
    submission_path.write_text(
        json.dumps({"submissions": submissions}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result = evaluate_submissions(
        manifest_path, submission_path, backend=backend, image=image
    )
    errors_by_task = {item["task_id"]: item["error"] for item in task_errors}
    for record in result["records"]:
        if record["method"] == method and record["task_id"] in errors_by_task:
            record["status"] = "infrastructure_error" if formal else "workflow_error"
            record["workflow_error"] = errors_by_task[record["task_id"]]
    for summary in result["methods"]:
        if summary["method"] == method:
            summary["status_counts"] = dict(Counter(
                record["status"] for record in result["records"]
                if record["method"] == method
            ))
    result["experiment_id"] = run_id
    result["method"] = method
    result["provider"] = provider
    result["workflow_mode"] = workflow_mode
    result["context_mode"] = context_mode
    result["execution_engine"] = execution_engine
    result["execution_engine_version"] = (
        package_version("langgraph") if execution_engine == "langgraph" else None
    )
    result["model_config"] = {
        "model": getattr(locator_client, "model_name", "scripted-mock"),
        "thinking_mode": getattr(locator_client, "thinking_mode", None),
        "reasoning_effort": getattr(locator_client, "reasoning_effort", None),
        "max_completion_tokens_per_call": getattr(
            locator_client, "max_output_tokens", None
        ),
        "timeout_seconds": getattr(locator_client, "timeout_seconds", None),
        "max_model_calls_total": max_model_calls,
        "max_submission_attempts": max_submission_attempts if formal else None,
    }
    result["model_calls_attempted_total"] = budget.used
    result["implementation_fingerprint"] = implementation_fingerprint
    result["dataset_fingerprint"] = dataset_fingerprint
    result["usage_records"] = usage_records
    result["task_errors"] = task_errors
    result["retry_records"] = retry_records
    result["experiment_protocol"] = "formal-v1" if formal else "legacy"
    if formal:
        selected_ids = {task.id for task in tasks}
        selected_records = [
            record for record in result["records"]
            if record["method"] == method and record["task_id"] in selected_ids
        ]
        submitted = sum("patch" in entry for entry in submissions)
        valid = sum(
            record["public_test_status"] is not None
            for record in selected_records
        )
        passed = sum(record["status"] == "passed" for record in selected_records)
        # A formal round is complete only when every selected task has an
        # evaluable patch. Exhausted retries are retained, not scored as tests.
        incomplete = valid != len(tasks) or any(record["status"] in {
            "infrastructure_error", "error", "invalid_patch", "rejected_patch", "missing_patch"
        } for record in selected_records)
        result["formal_status"] = (
            "incomplete" if incomplete
            else "partial" if limit is not None or task_ids is not None
            else "complete"
        )
        result["preflight"] = preflight
        result["submission_metrics"] = {
            "selected_tasks": len(tasks),
            "submitted_patches": submitted,
            "valid_patches": valid,
            "passed_patches": passed,
            "valid_submission_rate": round(valid / len(tasks), 4),
            "conditional_acceptance_rate": round(passed / valid, 4) if valid else None,
            "end_to_end_success_rate": round(passed / len(tasks), 4),
        }
    result["partial_run"] = limit is not None or task_ids is not None
    result["selected_task_count"] = len(tasks)
    result["submissions_path"] = str(submission_path)
    result_path = directory / "evaluation.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "directory": str(directory),
        "evaluation_path": str(result_path),
        "summary": result["methods"],
        "task_errors": task_errors,
        "formal_status": result.get("formal_status"),
        "submission_metrics": result.get("submission_metrics"),
    }
