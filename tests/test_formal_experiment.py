"""Formal experiments separate patch admissibility from test acceptance."""

import json
import shutil
from pathlib import Path

import pytest

from repofix import experiments
from repofix.cli import main
from repofix.experiments import run_experiment
from repofix.llm import ScriptedModelClient, ScriptedSequenceClient


ROOT = Path(__file__).resolve().parents[1]
DIRECT = ROOT / "examples/pagination-direct-response.json"


def _single_task_manifest(tmp_path: Path) -> Path:
    benchmark = tmp_path / "benchmark"
    task = benchmark / "tasks/pagination-zero-page-size"
    shutil.copytree(
        ROOT / "benchmarks/tasks/pagination-zero-page-size", task
    )
    manifest = benchmark / "manifest.yaml"
    manifest.write_text(
        "version: 1\n"
        "suite: formal-smoke\n"
        "language: python\n"
        "test_framework: pytest\n"
        "tasks:\n"
        "  - id: pagination-zero-page-size\n"
        "    config: tasks/pagination-zero-page-size/task.yaml\n"
        "    category: parameter-validation\n"
        "    status: ready\n",
        encoding="utf-8",
    )
    return manifest


def _evaluation(result: dict[str, object]) -> dict[str, object]:
    return json.loads(Path(result["evaluation_path"]).read_text(encoding="utf-8"))


def test_formal_preflight_stops_before_model_calls_or_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _single_task_manifest(tmp_path)
    client = ScriptedModelClient(DIRECT.read_text(encoding="utf-8"))
    monkeypatch.setattr(experiments, "audit_task", lambda task, **kwargs: {
        "task_id": task.id, "ready": False, "reason": "reference failed",
        "initial_status": "failed", "reference_status": "failed",
    })
    output = tmp_path / "runs"

    with pytest.raises(ValueError, match="formal preflight failed"):
        run_experiment(
            manifest_path=manifest, method="formal-preflight",
            locator_client=client, repair_client=client, output_root=output,
            backend="host", provider="mock", workflow_mode="single-agent",
            formal=True, max_model_calls=2,
        )

    assert client.calls == []
    assert not output.exists()


def test_formal_retry_recovers_protocol_error_then_grades_patch(tmp_path: Path) -> None:
    manifest = _single_task_manifest(tmp_path)
    client = ScriptedSequenceClient((
        '{"unexpected": true}', DIRECT.read_text(encoding="utf-8")
    ))

    result = run_experiment(
        manifest_path=manifest, method="formal-retry",
        locator_client=client, repair_client=client,
        output_root=tmp_path / "runs", backend="host", provider="mock",
        workflow_mode="single-agent", formal=True,
        max_submission_attempts=2, max_model_calls=2,
    )
    evaluation = _evaluation(result)

    assert evaluation["formal_status"] == "complete"
    assert evaluation["preflight"][0]["ready"] is True
    assert evaluation["records"][0]["status"] == "passed"
    assert evaluation["submission_metrics"]["valid_submission_rate"] == 1.0
    assert evaluation["submission_metrics"]["conditional_acceptance_rate"] == 1.0
    assert evaluation["retry_records"][0]["kind"] == "workflow_exception"
    assert len(client.calls) == 2
    assert "previous_submission_feedback" in json.loads(client.calls[1][1].content)


def test_formal_retry_rejects_noop_before_official_grading(tmp_path: Path) -> None:
    manifest = _single_task_manifest(tmp_path)
    answer = json.loads(DIRECT.read_text(encoding="utf-8"))
    answer["repair"]["new_snippet"] = answer["repair"]["old_snippet"]
    client = ScriptedSequenceClient((
        json.dumps(answer), DIRECT.read_text(encoding="utf-8")
    ))

    evaluation = _evaluation(run_experiment(
        manifest_path=manifest, method="formal-noop-retry",
        locator_client=client, repair_client=client,
        output_root=tmp_path / "runs", backend="host", provider="mock",
        workflow_mode="single-agent", formal=True,
        max_submission_attempts=2, max_model_calls=2,
    ))

    assert evaluation["records"][0]["status"] == "passed"
    assert evaluation["retry_records"][0]["kind"] == "invalid_candidate"
    assert "no change" in evaluation["retry_records"][0]["reason"]
    assert evaluation["usage_records"][0]["model_calls"] == 2


def test_formal_dynamic_counts_relocalization_against_call_budget(tmp_path: Path) -> None:
    manifest = _single_task_manifest(tmp_path)
    good = json.loads((ROOT / "examples/pagination-repair-response.json").read_text(encoding="utf-8"))
    bad = dict(good)
    bad["replacement_source"] = good["replacement_source"].replace(
        "if page_size <= 0:", "if page_size == -1:"
    )
    locator = ScriptedModelClient(
        (ROOT / "examples/pagination-locator-response.json").read_text(encoding="utf-8")
    )
    repair = ScriptedSequenceClient((json.dumps(bad), json.dumps(good)))

    evaluation = _evaluation(run_experiment(
        manifest_path=manifest, method="formal-dynamic-relocalize",
        locator_client=locator, repair_client=repair,
        output_root=tmp_path / "runs", backend="host", provider="mock",
        workflow_mode="dynamic", formal=True, max_model_calls=4,
    ))

    assert evaluation["formal_status"] == "complete"
    assert evaluation["records"][0]["status"] == "passed"
    assert evaluation["model_calls_attempted_total"] == 4
    assert evaluation["usage_records"][0]["model_calls"] == 4
    assert len(locator.calls) == len(repair.calls) == 2


def test_formal_public_test_failure_is_valid_submission(tmp_path: Path) -> None:
    manifest = _single_task_manifest(tmp_path)
    answer = json.loads(DIRECT.read_text(encoding="utf-8"))
    answer["repair"]["new_snippet"] = (
        "    total_pages = (len(items) + page_size - 1) // max(page_size, 1)\n"
    )
    client = ScriptedModelClient(json.dumps(answer))

    evaluation = _evaluation(run_experiment(
        manifest_path=manifest, method="formal-public-failed",
        locator_client=client, repair_client=client,
        output_root=tmp_path / "runs", backend="host", provider="mock",
        workflow_mode="single-agent", formal=True,
        max_submission_attempts=2, max_model_calls=2,
    ))

    assert evaluation["formal_status"] == "complete"
    assert evaluation["records"][0]["status"] == "failed"
    assert evaluation["records"][0]["public_test_status"] == "failed"
    assert evaluation["records"][0]["holdout_test_status"] is None
    assert evaluation["submission_metrics"]["valid_patches"] == 1
    assert evaluation["submission_metrics"]["conditional_acceptance_rate"] == 0.0
    assert evaluation["usage_records"][0]["model_calls"] == 1


def test_formal_exhausted_no_patch_is_terminal_but_not_a_test_failure(tmp_path: Path) -> None:
    manifest = _single_task_manifest(tmp_path)
    no_patch = json.dumps({
        "diagnosis": {"status": "insufficient_evidence", "explanation": "Need more evidence"},
        "repair": {"status": "no_patch", "explanation": "No safe change"},
    })
    client = ScriptedModelClient(no_patch)

    evaluation = _evaluation(run_experiment(
        manifest_path=manifest, method="formal-no-patch",
        locator_client=client, repair_client=client,
        output_root=tmp_path / "runs", backend="host", provider="mock",
        workflow_mode="single-agent", formal=True,
        max_submission_attempts=2, max_model_calls=2,
    ))

    assert evaluation["formal_status"] == "incomplete"
    assert evaluation["records"][0]["status"] == "no_submission"
    assert evaluation["records"][0]["public_test_status"] is None
    assert evaluation["submission_metrics"]["valid_patches"] == 0
    assert evaluation["submission_metrics"]["conditional_acceptance_rate"] is None
    assert evaluation["usage_records"][0]["model_calls"] == 2


def test_formal_service_failure_marks_run_incomplete(tmp_path: Path) -> None:
    manifest = _single_task_manifest(tmp_path)
    client = ScriptedSequenceClient(())

    evaluation = _evaluation(run_experiment(
        manifest_path=manifest, method="formal-outage",
        locator_client=client, repair_client=client,
        output_root=tmp_path / "runs", backend="host", provider="mock",
        workflow_mode="single-agent", formal=True,
        max_submission_attempts=2, max_model_calls=2,
    ))

    assert evaluation["formal_status"] == "incomplete"
    assert evaluation["records"][0]["status"] == "infrastructure_error"
    assert evaluation["records"][0]["public_test_status"] is None
    assert evaluation["task_errors"][0]["error_type"] == "InfrastructureError"
    assert evaluation["model_calls_attempted_total"] == 2


def test_formal_cli_runs_without_api_and_writes_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _single_task_manifest(tmp_path)
    status = main([
        "experiment", "--manifest", str(manifest),
        "--method", "formal-cli-smoke", "--provider", "mock",
        "--mock-response", str(DIRECT), "--workflow", "single-agent",
        "--runner", "host", "--formal", "--max-submission-attempts", "2",
        "--max-model-calls", "2", "--output-root", str(tmp_path / "runs"),
    ])

    assert status == 0
    evaluation = _evaluation(json.loads(capsys.readouterr().out))
    assert evaluation["formal_status"] == "complete"
    assert evaluation["submission_metrics"]["passed_patches"] == 1
