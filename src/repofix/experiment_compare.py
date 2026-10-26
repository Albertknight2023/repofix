"""Offline, apples-to-apples summaries of already completed experiments."""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def compare_evaluations(paths: tuple[Path, ...]) -> dict[str, object]:
    """Refuse partial or mismatched runs before computing descriptive metrics."""
    if len(paths) < 2:
        raise ValueError("compare requires at least two evaluation files")
    loaded: list[tuple[Path, dict[str, object], str, dict[str, dict[str, object]]]] = []
    baseline: tuple[object, ...] | None = None
    method_names: set[str] = set()
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"evaluation must be an object: {path}")
        if document.get("partial_run") or document.get("is_reference_fixture"):
            raise ValueError(f"partial runs and reference fixtures are not comparable: {path}")
        if document.get("experiment_protocol") == "formal-v1" and document.get("formal_status") != "complete":
            raise ValueError(f"incomplete formal experiment is not comparable: {path}")
        methods = document.get("methods")
        records = document.get("records")
        model = document.get("model_config")
        if (
            not isinstance(methods, list) or len(methods) != 1
            or not isinstance(methods[0], dict)
            or not isinstance(records, list) or not isinstance(model, dict)
            or not isinstance(document.get("manifest"), str)
            or not isinstance(document.get("backend"), str)
            or not isinstance(document.get("provider"), str)
            or not isinstance(document.get("implementation_fingerprint"), str)
            or not isinstance(document.get("dataset_fingerprint"), str)
        ):
            raise ValueError(f"not a complete Agent experiment: {path}")
        method = methods[0].get("method")
        if not isinstance(method, str) or method in method_names:
            raise ValueError("comparison methods must have unique names")
        method_names.add(method)
        by_task = {
            record["task_id"]: record for record in records
            if isinstance(record, dict)
            and record.get("method") == method
            and isinstance(record.get("task_id"), str)
        }
        if len(by_task) != len(records) or len(by_task) != document.get("task_count"):
            raise ValueError(f"records are missing, duplicated or from another method: {path}")
        cap = model.get("max_model_calls_total")
        calls = document.get("model_calls_attempted_total")
        # A global safety cap cannot affect a completed formal run when it
        # was never reached. This allows the final run to use the remaining
        # shared paid-call allowance without changing the scientific input.
        nonbinding_cap = (
            document.get("experiment_protocol") == "formal-v1"
            and document.get("formal_status") == "complete"
            and type(cap) is int and cap > 0
            and type(calls) is int and 0 <= calls < cap
        )
        identity = (
            str(Path(document["manifest"]).resolve()), document["backend"],
            document["provider"], model.get("model"),
            model.get("max_completion_tokens_per_call"),
            model.get("thinking_mode"),
            model.get("reasoning_effort"),
            model.get("timeout_seconds"),
            None if nonbinding_cap else cap,
            document.get("execution_engine", "legacy"),
            document.get("execution_engine_version"),
            document.get("experiment_protocol", "legacy"),
            model.get("max_submission_attempts"),
            document["implementation_fingerprint"],
            document["dataset_fingerprint"],
            frozenset(by_task),
        )
        if baseline is None:
            baseline = identity
        elif identity != baseline:
            raise ValueError("experiments have different task sets, backend, provider, engine or model budget")
        loaded.append((path.resolve(), document, method, by_task))

    rows: list[dict[str, object]] = []
    for path, document, method, by_task in loaded:
        statuses = Counter(str(record.get("status")) for record in by_task.values())
        usage = document.get("usage_records")
        usage = usage if isinstance(usage, list) else []
        submission_metrics = document.get("submission_metrics")
        submission_metrics = submission_metrics if isinstance(submission_metrics, dict) else {}
        rows.append({
            "method": method,
            "workflow_mode": document.get("workflow_mode"),
            "context_mode": document.get("context_mode"),
            "execution_engine": document.get("execution_engine", "legacy"),
            "execution_engine_version": document.get("execution_engine_version"),
            "evaluation_path": str(path),
            "tasks": len(by_task),
            "passed": statuses["passed"],
            "success_rate": round(statuses["passed"] / len(by_task), 4),
            "valid_patches": submission_metrics.get("valid_patches"),
            "valid_submission_rate": submission_metrics.get("valid_submission_rate"),
            "conditional_acceptance_rate": submission_metrics.get("conditional_acceptance_rate"),
            "end_to_end_success_rate": submission_metrics.get("end_to_end_success_rate"),
            "status_counts": dict(statuses),
            "mean_model_calls": _mean([
                float(item["model_calls"]) for item in usage
                if isinstance(item, dict) and type(item.get("model_calls")) is int
            ]),
            "mean_prompt_tokens_reported": _mean([
                float(item["prompt_tokens"]) for item in usage
                if isinstance(item, dict) and type(item.get("prompt_tokens")) is int
            ]),
            "mean_completion_tokens_reported": _mean([
                float(item["completion_tokens"]) for item in usage
                if isinstance(item, dict) and type(item.get("completion_tokens")) is int
            ]),
            "usage_task_count": len(usage),
            "model_calls_attempted_total": document.get("model_calls_attempted_total"),
            "max_model_calls_total": document["model_config"].get("max_model_calls_total"),
        })
    common_model_config = dict(loaded[0][1]["model_config"])
    if len({row["max_model_calls_total"] for row in rows}) > 1:
        common_model_config["max_model_calls_total"] = None
    return {
        "schema_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_count": len(loaded[0][3]),
        "provider": loaded[0][1]["provider"],
        "experiment_protocol": loaded[0][1].get("experiment_protocol", "legacy"),
        "model_config": common_model_config,
        "methods": rows,
        "limitations": [
            "Descriptive comparison only; no statistical significance is claimed.",
            "Mock-provider results verify pipeline behavior, not model repair ability.",
            "Missing token counts are excluded from token averages, never treated as zero.",
            "Different global safety caps are comparable only when every formal run completed below its cap.",
        ],
    }
