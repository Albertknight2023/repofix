"""Reproducibility audits and fixed-budget offline patch comparisons."""

import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from repofix.benchmarks.loader import load_tasks
from repofix.benchmarks.models import TaskSpec
from repofix.execution.docker_runner import DEFAULT_IMAGE, DockerTestRunner
from repofix.execution.models import TestRunResult, TestRunStatus
from repofix.execution.runner import run_test_command
from repofix.grader import grade_saved_patch
from repofix.workspace.manager import isolated_workspace


TestRunner = Callable[[Path, str, float], TestRunResult]


def task_runner(task: TaskSpec, backend: str, image: str | None = None) -> TestRunner:
    if backend == "docker":
        return DockerTestRunner(image=image or task.container_image or DEFAULT_IMAGE)
    if backend == "host":
        return run_test_command
    raise ValueError("backend must be docker or host")


def audit_task(task: TaskSpec, *, backend: str = "docker", image: str | None = None) -> dict[str, object]:
    """Require the buggy public test to fail and reference to pass acceptance."""

    runner = task_runner(task, backend, image)
    with isolated_workspace(task) as workspace:
        initial = runner(workspace.repo_path, task.public_test_command, task.timeout_seconds)
    reference = task.task_root / "solution.patch"
    if not reference.is_file():
        return {
            "task_id": task.id, "ready": False, "initial_status": initial.status.value,
            "reference_status": "missing", "reason": "reference patch is missing",
        }
    grade = grade_saved_patch(task, reference, test_runner=runner)
    ready = initial.status is TestRunStatus.FAILED and grade.passed
    return {
        "task_id": task.id,
        "ready": ready,
        "initial_status": initial.status.value,
        "initial_failures": len(initial.testfailures),
        "reference_status": grade.status,
        "reference_public_status": grade.public_test_status.value if grade.public_test_status else None,
        "reference_holdout_status": grade.hidden_test_status.value if grade.hidden_test_status else None,
        "reason": "buggy failed and reference passed" if ready else "reproduction or reference acceptance failed",
    }


def evaluate_submissions(
    manifest_path: Path,
    submissions_path: Path,
    *,
    backend: str = "docker",
    image: str | None = None,
) -> dict[str, object]:
    """Grade each submitted patch; missing tasks count in each method's denominator."""

    manifest_path = manifest_path.resolve()
    submissions_path = submissions_path.resolve()
    tasks = load_tasks(manifest_path)
    if not tasks:
        raise ValueError("benchmark manifest has no ready tasks")
    data = json.loads(submissions_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("submissions"), list):
        raise ValueError("submissions file must contain a submissions list")
    task_by_id = {task.id: task for task in tasks}
    entries: dict[tuple[str, str], dict[str, object]] = {}
    methods: set[str] = set()
    for entry in data["submissions"]:
        if not isinstance(entry, dict):
            raise ValueError("each submission must be an object")
        method, task_id = entry.get("method"), entry.get("task_id")
        if not isinstance(method, str) or not method.strip() or not isinstance(task_id, str):
            raise ValueError("submission requires method and task_id")
        if task_id not in task_by_id:
            raise ValueError(f"submission has unknown or non-ready task: {task_id}")
        key = (method, task_id)
        if key in entries:
            raise ValueError(f"duplicate submission: {method} / {task_id}")
        entries[key] = entry
        methods.add(method)
    if not methods:
        raise ValueError("submissions file contains no methods")

    records: list[dict[str, object]] = []
    for method in sorted(methods):
        for task in tasks:
            entry = entries.get((method, task.id))
            patch = entry.get("patch") if entry else None
            record: dict[str, object] = {
                "method": method, "task_id": task.id,
                "dataset": "BugsInPy" if task.id.startswith("bugsinpy-") else "development",
                "status": "no_submission", "public_test_status": None,
                "holdout_test_status": None,
            }
            if entry and isinstance(entry.get("no_submission_reason"), str) and not patch:
                record["no_submission_reason"] = entry["no_submission_reason"]
            if isinstance(patch, str) and patch:
                patch_path = Path(patch)
                if not patch_path.is_absolute():
                    patch_path = submissions_path.parent / patch_path
                if not patch_path.is_file():
                    record["status"] = "missing_patch"
                else:
                    grade = grade_saved_patch(
                        task, patch_path, test_runner=task_runner(task, backend, image)
                    )
                    record.update({
                        "status": grade.status,
                        "public_test_status": grade.public_test_status.value if grade.public_test_status else None,
                        "holdout_test_status": grade.hidden_test_status.value if grade.hidden_test_status else None,
                    })
            records.append(record)

    summary = []
    for method in sorted(methods):
        own = [record for record in records if record["method"] == method]
        counts = Counter(str(record["status"]) for record in own)
        passed = counts["passed"]
        summary.append({
            "method": method,
            "total": len(tasks),
            "passed": passed,
            "success_rate": round(passed / len(tasks), 4),
            "status_counts": dict(counts),
        })
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": str(manifest_path),
        "backend": backend,
        "task_count": len(tasks),
        "methods": summary,
        "records": records,
        "limitations": [
            "Reference fixture submissions are pipeline checks, not model performance.",
            "A passing test suite does not prove semantic correctness.",
        ],
    }
