"""Dashboard jobs reuse the CLI and persist independent grading results."""

import time
from pathlib import Path

from repofix.dashboard.jobs import JobManager


ROOT = Path(__file__).resolve().parents[1]


def wait_for_job(manager: JobManager, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        assert job is not None
        if job["state"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_repair_and_grade_jobs_with_mock_model(tmp_path: Path) -> None:
    manager = JobManager(ROOT)
    task = ROOT / "benchmarks/tasks/pagination-zero-page-size/task.yaml"
    repair = manager.submit("repair", [
        "repair", "--task", str(task), "--provider", "mock", "--runner", "host",
        "--mock-response", str(ROOT / "examples/pagination-locator-response.json"),
        "--mock-repair-response", str(ROOT / "examples/pagination-repair-response.json"),
        "--output-root", str(tmp_path),
    ], {"task_id": "pagination-zero-page-size"}, 30)
    repaired = wait_for_job(manager, str(repair["id"]))
    assert repaired["state"] == "succeeded", repaired["error"]
    assert repaired["result"]["public_tests_passed"] is True
    patch = Path(repaired["result"]["patch_path"])
    assert patch.is_file()

    grade = manager.submit("grade", [
        "grade", "--task", str(task), "--patch", str(patch), "--runner", "host",
    ], {"task_id": "pagination-zero-page-size"}, 30)
    graded = wait_for_job(manager, str(grade["id"]))
    assert graded["state"] == "succeeded", graded["error"]
    assert graded["result"]["hidden_test_status"] == "passed"
    assert (patch.parent / "grade.json").is_file()
