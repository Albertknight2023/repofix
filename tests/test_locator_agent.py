"""Exercise the first agent from a real public-test failure to a checked reply."""

import json
from pathlib import Path

import pytest

from repofix.agents import LocatorAgent
from repofix.agents.workflow import run_locator_task
from repofix.analysis import build_source_index
from repofix.benchmarks.loader import load_task
from repofix.execution.runner import run_test_command
from repofix.llm import ScriptedModelClient
from repofix.workspace.manager import isolated_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = (
    PROJECT_ROOT
    / "benchmarks"
    / "tasks"
    / "pagination-zero-page-size"
    / "task.yaml"
)
MOCK_RESPONSE_PATH = (
    PROJECT_ROOT / "examples" / "pagination-locator-response.json"
)


def test_locator_agent_receives_checked_public_evidence() -> None:
    client = ScriptedModelClient(
        MOCK_RESPONSE_PATH.read_text(encoding="utf-8")
    )

    run = run_locator_task(load_task(TASK_PATH), client)

    assert run.public_test_failures == 1
    assert run.locator_result.status == "hypothesis"
    assert run.locator_result.hypothesis is not None
    assert run.locator_result.hypothesis.target_file == Path(
        "src/pagination.py"
    )
    assert run.locator_result.hypothesis.target_symbol == "paginate"
    assert len(client.calls) == 1

    system_message, user_message = client.calls[0]
    assert "JSON" in system_message.content
    supplied = json.loads(user_message.content)
    evidence = supplied["evidence"]
    assert {"F1", "L1", "S1", "T1"}.issubset(
        {item["evidence_id"] for item in evidence}
    )
    assert "ZeroDivisionError" in evidence[0]["content"]
    assert "page_size=0" in next(
        item["content"] for item in evidence if item["evidence_id"] == "T1"
    )
    assert "grader_tests" not in user_message.content
    assert "reference" not in user_message.content
    assert "solution.patch" not in user_message.content


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"target_file": "tests/test_pagination.py"}, "matching source"),
        ({"target_symbol": "missing_function"}, "matching source"),
        ({"evidence_ids": ["F1", "UNKNOWN", "S1"]}, "unknown evidence"),
        ({"evidence_ids": ["F1", "L1"]}, "matching source"),
        ({"confidence": 1.5}, "confidence"),
    ],
)
def test_locator_rejects_unverified_model_claims(
    changes: dict[str, object],
    message: str,
) -> None:
    task = load_task(TASK_PATH)
    response = json.loads(MOCK_RESPONSE_PATH.read_text(encoding="utf-8"))
    response.update(changes)
    client = ScriptedModelClient(json.dumps(response))

    with isolated_workspace(task) as workspace:
        test_result = run_test_command(
            workspace.repo_path,
            task.public_test_command,
            task.timeout_seconds,
        )
        source_index = build_source_index(workspace.repo_path)
        with pytest.raises(ValueError, match=message):
            LocatorAgent(client).diagnose(
                issue=task.issue,
                test_result=test_result,
                source_index=source_index,
            )


def test_locator_accepts_explicit_insufficient_evidence() -> None:
    task = load_task(TASK_PATH)
    client = ScriptedModelClient(
        '{"status":"insufficient_evidence","explanation":"Need another failing test."}'
    )

    run = run_locator_task(task, client)

    assert run.locator_result.status == "insufficient_evidence"
    assert run.locator_result.hypothesis is None
    assert "Need another failing test" in run.locator_result.explanation


def test_locator_derives_only_missing_handoff_from_checked_target() -> None:
    answer = json.loads(MOCK_RESPONSE_PATH.read_text(encoding="utf-8"))
    answer.pop("next_step")
    client = ScriptedModelClient(json.dumps(answer))

    run = run_locator_task(load_task(TASK_PATH), client)

    hypothesis = run.locator_result.hypothesis
    assert hypothesis is not None
    assert hypothesis.next_step == "Inspect src/pagination.py::paginate."
    assert "derived" in run.locator_result.explanation
