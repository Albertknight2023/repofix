"""The optional graph engine must preserve RepoFix's repair/validation contract."""

import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from repofix.agents.langgraph_workflow import run_repair_task_langgraph
from repofix.agents.workflow import run_repair_task
from repofix.benchmarks.loader import load_task
from repofix.cli import main
from repofix.llm import ScriptedModelClient, ScriptedSequenceClient


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "benchmarks/tasks/pagination-zero-page-size/task.yaml"
LOCATOR = ROOT / "examples/pagination-locator-response.json"
REPAIR = ROOT / "examples/pagination-repair-response.json"
DIRECT = ROOT / "examples/pagination-direct-response.json"
MANIFEST = ROOT / "benchmarks/manifest.yaml"


def _clients(mode: str):
    if mode == "single-agent":
        return ScriptedModelClient(DIRECT.read_text(encoding="utf-8")), None
    locator = ScriptedModelClient(LOCATOR.read_text(encoding="utf-8"))
    good = REPAIR.read_text(encoding="utf-8")
    if mode == "two-agent":
        return locator, ScriptedModelClient(good)
    bad = json.loads(good)
    bad["replacement_source"] = bad["replacement_source"].replace(
        "if page_size <= 0:", "if page_size == -1:"
    )
    return locator, ScriptedSequenceClient((json.dumps(bad), good))


@pytest.mark.parametrize(
    "mode", ("single-agent", "two-agent", "dynamic", "competitive")
)
def test_graph_matches_legacy_public_outcome(mode: str) -> None:
    task = load_task(TASK)
    original = (task.repo_path / "src/pagination.py").read_bytes()
    legacy_locator, legacy_repair = _clients(mode)
    graph_locator, graph_repair = _clients(mode)

    legacy = run_repair_task(
        task, legacy_locator, legacy_repair or legacy_locator, workflow_mode=mode
    )
    graph = run_repair_task_langgraph(
        task, graph_locator, graph_repair or graph_locator, workflow_mode=mode
    )

    assert graph.initial_public_test_status == legacy.initial_public_test_status
    assert graph.selected_attempt == legacy.selected_attempt
    assert graph.model_calls == legacy.model_calls
    assert [item.validation_result.status for item in graph.attempts] == [
        item.validation_result.status for item in legacy.attempts
    ]
    assert [item.action for item in graph.schedule] == [
        item.action for item in legacy.schedule
    ]
    assert graph.validation_result is not None
    assert graph.validation_result.public_tests_passed
    assert graph.validation_result.diff == legacy.validation_result.diff
    assert (task.repo_path / "src/pagination.py").read_bytes() == original


def test_cli_graph_engine_with_mock_model(tmp_path: Path, capsys) -> None:
    status = main([
        "repair", "--task", str(TASK), "--runner", "host",
        "--provider", "mock", "--engine", "langgraph",
        "--workflow", "two-agent", "--mock-response", str(LOCATOR),
        "--mock-repair-response", str(REPAIR),
        "--output-root", str(tmp_path / "runs"),
    ])
    result = json.loads(capsys.readouterr().out)
    assert status == 0
    assert result["execution_engine"] == "langgraph"
    assert result["validation_status"] == "public_tests_passed"
    assert Path(result["patch_path"]).is_file()
    report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
    assert report["execution_engine"] == "langgraph"


def test_graph_experiment_records_engine_without_paid_api(tmp_path: Path, capsys) -> None:
    status = main([
        "experiment", "--manifest", str(MANIFEST), "--runner", "host",
        "--provider", "mock", "--engine", "langgraph",
        "--workflow", "single-agent", "--mock-response", str(DIRECT),
        "--method", "langgraph-smoke", "--limit", "1",
        "--output-root", str(tmp_path / "experiments"),
    ])
    result = json.loads(capsys.readouterr().out)
    assert status == 0
    evaluation = json.loads(Path(result["evaluation_path"]).read_text(encoding="utf-8"))
    assert evaluation["execution_engine"] == "langgraph"
    assert isinstance(evaluation["execution_engine_version"], str)
    assert evaluation["selected_task_count"] == 1
    assert evaluation["usage_records"][0]["model_calls"] == 1


def test_competitive_graph_stops_after_bounded_duplicate_proposals() -> None:
    good = REPAIR.read_text(encoding="utf-8")
    task = load_task(TASK)
    legacy = run_repair_task(
        task, ScriptedModelClient(LOCATOR.read_text(encoding="utf-8")),
        ScriptedSequenceClient((good,) * 4), workflow_mode="competitive",
    )
    graph = run_repair_task_langgraph(
        task, ScriptedModelClient(LOCATOR.read_text(encoding="utf-8")),
        ScriptedSequenceClient((good,) * 4), workflow_mode="competitive",
    )
    assert graph.model_calls == legacy.model_calls == 5
    assert [item.validation_result.status for item in graph.attempts] == [
        item.validation_result.status for item in legacy.attempts
    ]
    assert graph.schedule[-2].action == "competition_incomplete"


def test_single_graph_can_abstain_without_inventing_patch() -> None:
    response = json.dumps({
        "diagnosis": {"status": "insufficient_evidence", "explanation": "Not enough evidence"},
        "repair": {"status": "no_patch", "explanation": "No safe change"},
    })
    run = run_repair_task_langgraph(
        load_task(TASK), ScriptedModelClient(response),
        ScriptedModelClient(response), workflow_mode="single-agent",
    )
    assert run.repair_result.candidate is None
    assert run.validation_result is None
    assert run.selected_attempt == 1
