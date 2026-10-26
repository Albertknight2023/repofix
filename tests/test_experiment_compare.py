"""Compare completed runs only after enforcing a shared benchmark budget."""

import json
from pathlib import Path

import pytest

from repofix.experiment_compare import compare_evaluations
from repofix.cli import main


def _write(path: Path, method: str, statuses: tuple[str, str], **overrides: object) -> Path:
    document = {
        "manifest": str(path.parent / "manifest.yaml"),
        "backend": "host", "provider": "mock", "partial_run": False,
        "task_count": 2, "methods": [{"method": method}],
        "model_config": {
            "model": "scripted-mock", "max_completion_tokens_per_call": 6000,
            "thinking_mode": None, "timeout_seconds": None,
        },
        "implementation_fingerprint": "implementation-v1",
        "dataset_fingerprint": "dataset-v1",
        "records": [
            {"method": method, "task_id": name, "status": status}
            for name, status in zip(("a", "b"), statuses)
        ],
        "usage_records": [
            {"task_id": "a", "model_calls": 2, "prompt_tokens": None, "completion_tokens": None},
            {"task_id": "b", "model_calls": 3, "prompt_tokens": 50, "completion_tokens": 20},
        ],
    }
    document.update(overrides)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_compare_counts_full_denominator_and_preserves_missing_usage(tmp_path: Path) -> None:
    first = _write(tmp_path / "first.json", "baseline", ("passed", "no_submission"))
    second = _write(tmp_path / "second.json", "dynamic", ("passed", "passed"))
    comparison = compare_evaluations((first, second))

    assert isinstance(comparison["created_at_utc"], str)
    assert comparison["task_count"] == 2
    assert [row["passed"] for row in comparison["methods"]] == [1, 2]
    assert comparison["methods"][0]["mean_model_calls"] == 2.5
    assert comparison["methods"][0]["mean_prompt_tokens_reported"] == 50


def test_cli_compares_saved_evaluations_without_model(tmp_path: Path, capsys) -> None:
    first = _write(tmp_path / "first.json", "baseline", ("passed", "failed"))
    second = _write(tmp_path / "second.json", "dynamic", ("passed", "passed"))
    output = tmp_path / "comparison.json"
    assert main(["compare", "--evaluations", str(first), str(second), "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["methods"][1]["success_rate"] == 1.0
    assert json.loads(capsys.readouterr().out)["output"] == str(output.resolve())


@pytest.mark.parametrize("change", [
    {"partial_run": True},
    {"backend": "docker"},
    {"task_count": 3},
    {"model_config": {"model": "other", "max_completion_tokens_per_call": 6000}},
    {"model_config": {"model": "scripted-mock", "max_completion_tokens_per_call": 6000, "thinking_mode": "disabled"}},
    {"implementation_fingerprint": "implementation-v2"},
    {"dataset_fingerprint": "dataset-v2"},
    {"execution_engine": "langgraph", "execution_engine_version": "1.2.17"},
])
def test_compare_rejects_unfair_inputs(tmp_path: Path, change: dict[str, object]) -> None:
    first = _write(tmp_path / "first.json", "baseline", ("passed", "failed"))
    second = _write(tmp_path / "second.json", "dynamic", ("passed", "passed"), **change)
    with pytest.raises(ValueError):
        compare_evaluations((first, second))


def test_compare_rejects_incomplete_formal_run(tmp_path: Path) -> None:
    first = _write(
        tmp_path / "first.json", "baseline", ("passed", "no_submission"),
        experiment_protocol="formal-v1", formal_status="incomplete",
    )
    second = _write(
        tmp_path / "second.json", "dynamic", ("passed", "passed"),
        experiment_protocol="formal-v1", formal_status="complete",
    )
    with pytest.raises(ValueError, match="incomplete formal"):
        compare_evaluations((first, second))


def test_compare_exposes_formal_conditional_acceptance(tmp_path: Path) -> None:
    metrics = {
        "valid_patches": 1, "valid_submission_rate": 0.5,
        "conditional_acceptance_rate": 1.0, "end_to_end_success_rate": 0.5,
    }
    first = _write(
        tmp_path / "first.json", "baseline", ("passed", "no_submission"),
        experiment_protocol="formal-v1", formal_status="complete",
        submission_metrics=metrics,
    )
    second = _write(
        tmp_path / "second.json", "dynamic", ("passed", "passed"),
        experiment_protocol="formal-v1", formal_status="complete",
        submission_metrics={
            "valid_patches": 2, "valid_submission_rate": 1.0,
            "conditional_acceptance_rate": 1.0, "end_to_end_success_rate": 1.0,
        },
    )
    comparison = compare_evaluations((first, second))

    assert comparison["experiment_protocol"] == "formal-v1"
    assert comparison["methods"][0]["conditional_acceptance_rate"] == 1.0
    assert comparison["methods"][1]["valid_submission_rate"] == 1.0


def test_compare_allows_different_nonbinding_formal_safety_caps(tmp_path: Path) -> None:
    first = _write(
        tmp_path / "first.json", "single", ("passed", "failed"),
        experiment_protocol="formal-v1", formal_status="complete",
        model_calls_attempted_total=2,
        model_config={
            "model": "scripted-mock", "max_completion_tokens_per_call": 6000,
            "thinking_mode": None, "timeout_seconds": None,
            "max_model_calls_total": 5,
        },
    )
    second = _write(
        tmp_path / "second.json", "dynamic", ("passed", "passed"),
        experiment_protocol="formal-v1", formal_status="complete",
        model_calls_attempted_total=3,
        model_config={
            "model": "scripted-mock", "max_completion_tokens_per_call": 6000,
            "thinking_mode": None, "timeout_seconds": None,
            "max_model_calls_total": 4,
        },
    )
    comparison = compare_evaluations((first, second))
    assert comparison["model_config"]["max_model_calls_total"] is None
    assert [row["max_model_calls_total"] for row in comparison["methods"]] == [5, 4]


def test_compare_rejects_different_binding_formal_safety_caps(tmp_path: Path) -> None:
    first = _write(
        tmp_path / "first.json", "single", ("passed", "failed"),
        experiment_protocol="formal-v1", formal_status="complete",
        model_calls_attempted_total=5,
        model_config={
            "model": "scripted-mock", "max_completion_tokens_per_call": 6000,
            "thinking_mode": None, "timeout_seconds": None,
            "max_model_calls_total": 5,
        },
    )
    second = _write(
        tmp_path / "second.json", "dynamic", ("passed", "passed"),
        experiment_protocol="formal-v1", formal_status="complete",
        model_calls_attempted_total=3,
        model_config={
            "model": "scripted-mock", "max_completion_tokens_per_call": 6000,
            "thinking_mode": None, "timeout_seconds": None,
            "max_model_calls_total": 4,
        },
    )
    with pytest.raises(ValueError, match="different task sets"):
        compare_evaluations((first, second))
