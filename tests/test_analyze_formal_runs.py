"""Offline formal-run analysis must follow the submitted patch, not old retries."""

import json
from pathlib import Path

from scripts.analyze_formal_runs import analyze


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _experiment(root: Path, method: str, passed: bool) -> Path:
    task_id = "bugsinpy-tqdm-1"
    run_dir = root / "runs" / "selected"
    run_dir.mkdir(parents=True)
    patch = run_dir / "candidate.patch"
    patch.write_text("--- a/x\n+++ b/x\n", encoding="utf-8")
    public_status = "passed" if passed else "failed"
    _write_json(
        run_dir / "report.json",
        {
            "task": {"id": task_id},
            "validation": {
                "public_test": {
                    "status": public_status,
                    "failures": [] if passed else [{"exception_type": "AssertionError"}],
                }
            },
        },
    )
    _write_json(root / "submissions.json", {"submissions": [{"task_id": task_id, "patch": str(patch)}]})
    _write_json(
        root / "evaluation.json",
        {
            "formal_status": "complete",
            "method": method,
            "implementation_fingerprint": "same-code",
            "dataset_fingerprint": "same-data",
            "selected_task_count": 1,
            "task_count": 1,
            "submission_metrics": {"valid_patches": 1},
            "model_calls_attempted_total": 1,
            "records": [
                {
                    "task_id": task_id,
                    "status": "passed" if passed else "failed",
                    "public_test_status": public_status,
                    "holdout_test_status": "passed" if passed else None,
                }
            ],
            "usage_records": [
                {
                    "task_id": task_id,
                    "model_calls": 1,
                    "model_calls_succeeded": 1,
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "repair_seconds": 2.0,
                    "candidate_count": 1,
                    "selected_attempt": 1,
                }
            ],
        },
    )
    return root / "evaluation.json"


def test_analysis_uses_submitted_run_when_older_retry_exists(tmp_path: Path) -> None:
    first = _experiment(tmp_path / "first", "single", False)
    second = _experiment(tmp_path / "second", "dynamic", True)
    _write_json(
        second.parent / "runs" / "discarded" / "report.json",
        {"task": {"id": "bugsinpy-tqdm-1"}, "validation": {}},
    )

    result = analyze([first, second])

    assert result["methods"][0]["outcomes"] == {"public_failed": 1}
    assert result["methods"][1]["outcomes"] == {"passed": 1}
    assert result["methods"][0]["public_failure_exception_types_by_task"] == {"AssertionError": 1}
    assert result["paired_comparisons"][0]["right_gains"] == ["bugsinpy-tqdm-1"]
