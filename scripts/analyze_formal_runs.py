"""Summarize completed formal experiments without invoking models or tests.

Usage::

    python scripts/analyze_formal_runs.py EVALUATION_1 EVALUATION_2 EVALUATION_3

The command prints machine-readable JSON. It reads each evaluation, its usage
records, and the run reports beneath the same experiment directory. It does
not access hidden test source or infer a root cause from a failure status.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _task_project(task_id: str) -> str:
    if not task_id.startswith("bugsinpy-") or "-" not in task_id[9:]:
        raise ValueError(f"Unexpected BugsInPy task ID: {task_id}")
    return task_id.removeprefix("bugsinpy-").rsplit("-", 1)[0]


def _outcome(record: dict[str, Any]) -> str:
    if record["status"] == "passed":
        if record["public_test_status"] != "passed" or record["holdout_test_status"] != "passed":
            raise ValueError(f"Inconsistent passed record: {record['task_id']}")
        return "passed"
    if record["public_test_status"] == "failed":
        return "public_failed"
    if record["public_test_status"] == "passed" and record["holdout_test_status"] == "failed":
        return "holdout_failed"
    raise ValueError(f"Unexpected formal outcome: {record}")


def _indexed(records: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        task_id = record["task_id"]
        if task_id in result:
            raise ValueError(f"Duplicate {label}: {task_id}")
        result[task_id] = record
    return result


def analyze(paths: list[Path]) -> dict[str, Any]:
    if len(paths) < 2:
        raise ValueError("Provide at least two completed evaluations")

    methods: list[dict[str, Any]] = []
    task_sets: list[set[str]] = []
    fingerprints: set[tuple[str, str]] = set()
    names: set[str] = set()

    for path in paths:
        evaluation = _read_json(path)
        if evaluation.get("formal_status") != "complete":
            raise ValueError(f"Not a complete formal experiment: {path}")
        method = evaluation["method"]
        if method in names:
            raise ValueError(f"Duplicate method: {method}")
        names.add(method)
        fingerprints.add((evaluation["implementation_fingerprint"], evaluation["dataset_fingerprint"]))

        records = _indexed(evaluation["records"], "evaluation record")
        usage = _indexed(evaluation["usage_records"], "usage record")
        # Failed admissibility attempts can leave several run reports for one
        # task. The submitted patch points to the report for the evaluated run.
        submissions = _indexed(
            _read_json(path.parent / "submissions.json")["submissions"],
            "submission",
        )
        reports: dict[str, dict[str, Any]] = {}
        for task_id, submission in submissions.items():
            patch_path = Path(submission["patch"]).resolve()
            if not patch_path.is_relative_to((path.parent / "runs").resolve()):
                raise ValueError(f"Submitted patch is outside experiment runs: {task_id}")
            if not patch_path.is_file():
                raise ValueError(f"Submitted patch is missing: {task_id}")
            report = _read_json(patch_path.parent / "report.json")
            if report["task"]["id"] != task_id:
                raise ValueError(f"Submission and run report disagree: {task_id}")
            reports[task_id] = report
        task_ids = set(records)
        if task_ids != set(usage) or task_ids != set(reports):
            raise ValueError(f"Records, usage and run reports disagree: {path}")
        if len(task_ids) != evaluation["selected_task_count"] or len(task_ids) != evaluation["task_count"]:
            raise ValueError(f"Task count mismatch: {path}")
        if evaluation["submission_metrics"]["valid_patches"] != len(task_ids):
            raise ValueError(f"Not every task submitted a valid patch: {path}")
        task_sets.append(task_ids)

        rows = []
        for task_id in sorted(task_ids):
            record, item, report = records[task_id], usage[task_id], reports[task_id]
            outcome = _outcome(record)
            public_test = report.get("validation", {}).get("public_test") or {}
            if outcome == "public_failed" and public_test.get("status") != "failed":
                raise ValueError(f"Public test status disagrees with run report: {task_id}")
            exceptions = sorted(
                {
                    failure.get("exception_type") or "unknown"
                    for failure in public_test.get("failures", [])
                }
            )
            rows.append(
                {
                    "task_id": task_id,
                    "project": _task_project(task_id),
                    "outcome": outcome,
                    "model_calls": item["model_calls"],
                    "model_calls_succeeded": item["model_calls_succeeded"],
                    "prompt_tokens": item["prompt_tokens"],
                    "completion_tokens": item["completion_tokens"],
                    "repair_seconds": item["repair_seconds"],
                    "candidate_count": item["candidate_count"],
                    "selected_attempt": item["selected_attempt"],
                    "public_exception_types": exceptions if outcome == "public_failed" else [],
                }
            )

        by_project: dict[str, dict[str, int]] = {}
        for row in rows:
            tally = by_project.setdefault(row["project"], {"tasks": 0, "passed": 0})
            tally["tasks"] += 1
            tally["passed"] += row["outcome"] == "passed"
        exception_counts = Counter(
            exception
            for row in rows
            if row["outcome"] == "public_failed"
            for exception in set(row["public_exception_types"])
        )
        total_calls = sum(row["model_calls"] for row in rows)
        successful_calls = sum(row["model_calls_succeeded"] for row in rows)
        if total_calls != evaluation["model_calls_attempted_total"]:
            raise ValueError(f"Model call total mismatch: {path}")
        if successful_calls > total_calls:
            raise ValueError(f"Successful model calls exceed attempts: {path}")
        methods.append(
            {
                "method": method,
                "evaluation_path": str(path.resolve()),
                "task_count": len(rows),
                "outcomes": dict(sorted(Counter(row["outcome"] for row in rows).items())),
                "by_project": dict(sorted(by_project.items())),
                "model_calls": total_calls,
                "model_calls_succeeded": successful_calls,
                "attempts_without_success": total_calls - successful_calls,
                "prompt_tokens": sum(row["prompt_tokens"] for row in rows),
                "completion_tokens": sum(row["completion_tokens"] for row in rows),
                "repair_seconds_total": round(sum(row["repair_seconds"] for row in rows), 3),
                "repair_seconds_median": round(statistics.median(row["repair_seconds"] for row in rows), 3),
                "tasks_with_second_candidate": sum(row["candidate_count"] > 1 for row in rows),
                "public_failure_exception_types_by_task": dict(sorted(exception_counts.items())),
                "rows": rows,
            }
        )

    if len(fingerprints) != 1 or any(task_set != task_sets[0] for task_set in task_sets[1:]):
        raise ValueError("Implementation, dataset or task sets differ; paired analysis is invalid")
    pairs = []
    for left, right in combinations(methods, 2):
        left_rows = {row["task_id"]: row for row in left["rows"]}
        right_rows = {row["task_id"]: row for row in right["rows"]}
        pairs.append(
            {
                "left": left["method"],
                "right": right["method"],
                "right_gains": sorted(
                    task_id
                    for task_id in left_rows
                    if left_rows[task_id]["outcome"] != "passed" and right_rows[task_id]["outcome"] == "passed"
                ),
                "right_losses": sorted(
                    task_id
                    for task_id in left_rows
                    if left_rows[task_id]["outcome"] == "passed" and right_rows[task_id]["outcome"] != "passed"
                ),
            }
        )
    return {
        "task_count": len(task_sets[0]),
        "implementation_fingerprint": next(iter(fingerprints))[0],
        "dataset_fingerprint": next(iter(fingerprints))[1],
        "methods": methods,
        "paired_comparisons": pairs,
        "notes": [
            "repair_seconds measures the recorded repair workflow, not full experiment wall time or hidden grading time",
            "public exception types describe final visible test failures, not proven root causes",
            "hidden grading details are not available in the agent run reports",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evaluations", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.evaluations), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
