"""One-call baseline and call-graph ablation stay inside the same grader."""

import json
from pathlib import Path

import pytest

from repofix.agents.workflow import run_repair_task
from repofix.benchmarks.loader import load_task
from repofix.cli import main
from repofix.llm import ScriptedModelClient
from repofix.reporting import save_repair_run


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "benchmarks/tasks/pagination-zero-page-size/task.yaml"
MANIFEST = ROOT / "benchmarks/manifest.yaml"
DIRECT = ROOT / "examples/pagination-direct-response.json"
LOCATOR = ROOT / "examples/pagination-locator-response.json"
REPAIR = ROOT / "examples/pagination-repair-response.json"


def test_single_agent_uses_one_call_and_keeps_hidden_material_out(tmp_path: Path) -> None:
    task = load_task(TASK)
    original = (task.repo_path / "src/pagination.py").read_bytes()
    client = ScriptedModelClient(DIRECT.read_text(encoding="utf-8"))

    run = run_repair_task(task, client, client, workflow_mode="single-agent")
    saved = save_repair_run(task=task, run=run, output_root=tmp_path)
    report = json.loads(saved.report_path.read_text(encoding="utf-8"))

    assert len(client.calls) == 1
    assert run.validation_result is not None
    assert run.validation_result.public_tests_passed
    assert report["model_calls"] == 1
    assert report["workflow_mode"] == "single-agent"
    assert report["token_usage"]["repair_prompt"] is None
    assert saved.patch_path is not None
    assert (task.repo_path / "src/pagination.py").read_bytes() == original
    user_prompt = client.calls[0][1].content
    assert "ZeroDivisionError" in user_prompt
    eligible = json.loads(user_prompt)["eligible_targets"]
    assert any(
        item["target_file"] == "src/pagination.py"
        and item["target_symbol"] == "paginate"
        for item in eligible
    )
    assert "grader_tests" not in user_prompt
    assert "solution.patch" not in user_prompt


@pytest.mark.parametrize("field,value,message", [
    ("evidence_ids", ["F1", "UNKNOWN"], "unknown evidence"),
    ("target_file", "tests/test_pagination.py", "matching source"),
])
def test_single_agent_rejects_unsupported_diagnosis(
    field: str, value: object, message: str
) -> None:
    answer = json.loads(DIRECT.read_text(encoding="utf-8"))
    answer["diagnosis"][field] = value
    client = ScriptedModelClient(json.dumps(answer))
    with pytest.raises(ValueError, match=message):
        run_repair_task(load_task(TASK), client, client, workflow_mode="single-agent")
    assert len(client.calls) == 1


@pytest.mark.parametrize("field,value,message", [
    ("evidence_ids", ["UNKNOWN"], "invalid evidence IDs"),
    ("target_file", "tests/test_pagination.py", "differs from"),
])
def test_single_agent_rejects_unsupported_patch(
    field: str, value: object, message: str
) -> None:
    answer = json.loads(DIRECT.read_text(encoding="utf-8"))
    answer["repair"][field] = value
    client = ScriptedModelClient(json.dumps(answer))
    with pytest.raises(ValueError, match=message):
        run_repair_task(load_task(TASK), client, client, workflow_mode="single-agent")
    assert len(client.calls) == 1


def test_single_agent_can_abstain_without_fabricating_patch() -> None:
    client = ScriptedModelClient(json.dumps({
        "diagnosis": {"status": "insufficient_evidence", "explanation": "Need evidence"},
        "repair": {"status": "no_patch", "explanation": "No safe patch"},
    }))
    run = run_repair_task(load_task(TASK), client, client, workflow_mode="single-agent")
    assert len(client.calls) == 1
    assert run.locator_result.hypothesis is None
    assert run.repair_result.candidate is None
    assert run.validation_result is None


def test_traceback_only_ablation_omits_call_graph_context() -> None:
    locator = ScriptedModelClient(LOCATOR.read_text(encoding="utf-8"))
    repair = ScriptedModelClient(REPAIR.read_text(encoding="utf-8"))
    run = run_repair_task(
        load_task(TASK), locator, repair, context_mode="traceback-only"
    )
    assert run.context_mode == "traceback-only"
    assert run.related_context is None
    assert len(locator.calls) == len(repair.calls) == 1
    assert run.validation_result is not None
    assert run.validation_result.public_tests_passed
    assert not any(item.evidence_id.startswith("C") for item in run.locator_result.evidence)


def test_cli_experiment_single_agent_keeps_full_denominator(
    tmp_path: Path, capsys
) -> None:
    status = main([
        "experiment", "--manifest", str(MANIFEST),
        "--method", "single-agent-ast-smoke", "--workflow", "single-agent",
        "--provider", "mock", "--mock-response", str(DIRECT),
        "--runner", "host", "--limit", "1", "--output-root", str(tmp_path),
    ])
    assert status == 0
    output = json.loads(capsys.readouterr().out)
    evaluation = json.loads(Path(output["evaluation_path"]).read_text(encoding="utf-8"))
    assert evaluation["workflow_mode"] == "single-agent"
    assert evaluation["context_mode"] == "ast-callgraph"
    assert evaluation["partial_run"] is True
    assert evaluation["selected_task_count"] == 1
    assert evaluation["task_count"] == 5
    assert evaluation["methods"][0]["passed"] == 1
    assert evaluation["methods"][0]["total"] == 5
    assert evaluation["model_config"]["model"] == "scripted-mock"
    assert len(evaluation["implementation_fingerprint"]) == 64
    assert len(evaluation["dataset_fingerprint"]) == 64
    assert evaluation["usage_records"][0]["model_calls"] == 1


def test_experiment_marks_model_protocol_error_separately(
    tmp_path: Path, capsys
) -> None:
    bad_response = tmp_path / "bad-direct.json"
    bad_response.write_text('{"unexpected":true}', encoding="utf-8")
    status = main([
        "experiment", "--manifest", str(MANIFEST),
        "--method", "broken-pilot", "--workflow", "single-agent",
        "--provider", "mock", "--mock-response", str(bad_response),
        "--runner", "host", "--limit", "1", "--output-root", str(tmp_path / "runs"),
    ])
    assert status == 0
    output = json.loads(capsys.readouterr().out)
    evaluation = json.loads(Path(output["evaluation_path"]).read_text(encoding="utf-8"))
    counts = evaluation["methods"][0]["status_counts"]
    assert counts["workflow_error"] == 1
    assert counts["no_submission"] == 4
    assert evaluation["records"][0]["status"] == "workflow_error"
    assert evaluation["task_errors"][0]["task_id"] == "pagination-zero-page-size"
    assert evaluation["usage_records"][0]["status"] == "workflow_error"
    assert evaluation["usage_records"][0]["model_calls"] == 1


def test_experiment_can_select_one_registered_task_by_id(
    tmp_path: Path, capsys
) -> None:
    status = main([
        "experiment", "--manifest", str(MANIFEST),
        "--method", "selected-task-smoke", "--workflow", "single-agent",
        "--provider", "mock", "--mock-response", str(DIRECT),
        "--runner", "host", "--task-id", "pagination-zero-page-size",
        "--output-root", str(tmp_path),
    ])
    assert status == 0
    output = json.loads(capsys.readouterr().out)
    evaluation = json.loads(Path(output["evaluation_path"]).read_text(encoding="utf-8"))
    assert evaluation["partial_run"] is True
    assert evaluation["selected_task_count"] == 1
    assert evaluation["methods"][0]["passed"] == 1
    assert evaluation["methods"][0]["total"] == 5


def test_experiment_enforces_paid_call_budget(tmp_path: Path, capsys) -> None:
    status = main([
        "experiment", "--manifest", str(MANIFEST),
        "--method", "budget-smoke", "--workflow", "single-agent",
        "--provider", "mock", "--mock-response", str(DIRECT),
        "--runner", "host", "--limit", "2", "--max-model-calls", "1",
        "--output-root", str(tmp_path),
    ])
    assert status == 0
    output = json.loads(capsys.readouterr().out)
    evaluation = json.loads(Path(output["evaluation_path"]).read_text(encoding="utf-8"))
    assert evaluation["model_calls_attempted_total"] == 1
    assert evaluation["usage_records"][1]["model_calls"] == 0
    assert evaluation["task_errors"][0]["error"] == (
        "experiment model-call budget exhausted"
    )
