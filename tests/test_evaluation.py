"""Fixed-denominator benchmark evaluation and real-bug reproducibility."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from repofix.benchmarks.loader import load_task
from repofix.evaluation import audit_task, evaluate_submissions


ROOT = Path(__file__).resolve().parents[1]


def _real_image_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", "repofix-pytest:py38"],
            capture_output=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def test_evaluation_counts_missing_submission_as_not_passed(tmp_path: Path) -> None:
    patch = ROOT / "benchmarks/tasks/pagination-zero-page-size/solution.patch"
    submissions = tmp_path / "submissions.json"
    submissions.write_text(json.dumps({"submissions": [
        {"method": "fixture", "task_id": "pagination-zero-page-size", "patch": str(patch)}
    ]}), encoding="utf-8")
    result = evaluate_submissions(
        ROOT / "benchmarks/manifest.yaml", submissions, backend="host"
    )
    assert result["task_count"] == 5
    assert result["methods"][0]["passed"] == 1
    assert result["methods"][0]["total"] == 5
    assert sum(record["status"] == "no_submission" for record in result["records"]) == 4


def test_evaluation_preserves_reason_for_selected_no_submission(tmp_path: Path) -> None:
    submissions = tmp_path / "submissions.json"
    submissions.write_text(json.dumps({"submissions": [
        {
            "method": "fixture",
            "task_id": "pagination-zero-page-size",
            "no_submission_reason": "Candidate made no change.",
        }
    ]}), encoding="utf-8")

    result = evaluate_submissions(
        ROOT / "benchmarks/manifest.yaml", submissions, backend="host"
    )

    record = next(item for item in result["records"] if item["task_id"] == "pagination-zero-page-size")
    assert record["status"] == "no_submission"
    assert record["no_submission_reason"] == "Candidate made no change."


@pytest.mark.skipif(not _real_image_ready(), reason="real-task Docker image unavailable")
@pytest.mark.parametrize("task_id", [
    "bugsinpy-tqdm-1", "bugsinpy-tqdm-3", "bugsinpy-tqdm-4",
    "bugsinpy-tqdm-5", "bugsinpy-tqdm-6", "bugsinpy-tqdm-7", "bugsinpy-tqdm-8", "bugsinpy-PySnooper-3",
])
def test_real_bugs_fail_and_reference_passes(task_id: str) -> None:
    task = load_task(ROOT / "benchmarks/real/tasks" / task_id / "task.yaml")
    result = audit_task(task)
    assert result["ready"] is True
    assert result["initial_status"] == "failed"
    assert result["reference_holdout_status"] == "passed"
