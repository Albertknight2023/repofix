"""Every development task needs a failure, reference fix, and usable workflow."""

import ast
import json
import shutil
from pathlib import Path

import pytest

from repofix.agents import LocatorAgent
from repofix.agents.workflow import run_repair_task
from repofix.analysis import (
    build_call_graph,
    build_relevant_context,
    build_source_index,
)
from repofix.benchmarks.loader import load_tasks
from repofix.execution.models import TestRunStatus as RunStatus
from repofix.execution.runner import run_test_command
from repofix.grader import grade_saved_patch
from repofix.llm import ScriptedModelClient
from repofix.workspace.manager import isolated_workspace


MANIFEST_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "manifest.yaml"
TASKS = load_tasks(MANIFEST_PATH)


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_initial_fails_and_reference_patch_passes(task) -> None:
    with isolated_workspace(task) as workspace:
        initial = run_test_command(
            workspace.repo_path, task.public_test_command, task.timeout_seconds
        )
        assert initial.status is RunStatus.FAILED
        shutil.copytree(task.grader_test_path, workspace.root / "grader_tests")
        full_initial = run_test_command(
            workspace.repo_path, task.full_test_command, task.timeout_seconds
        )
        assert full_initial.status is RunStatus.FAILED

    with isolated_workspace(task) as workspace:
        shutil.copytree(
            task.task_root / "reference" / "src",
            workspace.repo_path / "src",
            dirs_exist_ok=True,
        )
        shutil.copytree(task.grader_test_path, workspace.root / "grader_tests")
        reference_source_result = run_test_command(
            workspace.repo_path, task.full_test_command, task.timeout_seconds
        )
        assert reference_source_result.status is RunStatus.PASSED

    reference = grade_saved_patch(task, task.task_root / "solution.patch")
    assert reference.passed


@pytest.mark.parametrize(
    "task_id, expected_symbol",
    [
        ("shipping-member-rule", "shipping_fee"),
        ("setting-key-casefold", "normalize_key"),
        ("asset-path-traversal", "asset_path"),
    ],
)
def test_assertion_failure_maps_unique_direct_callee(
    task_id: str, expected_symbol: str
) -> None:
    task = next(task for task in TASKS if task.id == task_id)
    with isolated_workspace(task) as workspace:
        failure_run = run_test_command(
            workspace.repo_path, task.public_test_command, task.timeout_seconds
        )
        index = build_source_index(workspace.repo_path)
        graph = build_call_graph(index)
        context = build_relevant_context(index, graph, failure_run.testfailures[0])
        assert context is not None
        assert context.target.name == expected_symbol
        assert context.selection_rule == (
            "unique direct production callee of failing public test"
        )

        reply = {
            "status": "hypothesis",
            "summary": "Test assertion differs from expected behavior.",
            "root_cause": "The target function returns the wrong value.",
            "target_file": context.target.relative_path.as_posix(),
            "target_symbol": context.target.qualified_name,
            "confidence": 0.7,
            "evidence_ids": ["F1", "S1"],
            "next_step": "Generate a focused replacement.",
        }
        result = LocatorAgent(ScriptedModelClient(json.dumps(reply))).diagnose(
            issue=task.issue,
            test_result=failure_run,
            source_index=index,
            related_context=context,
        )
        assert result.hypothesis is not None
        assert result.hypothesis.target_symbol == expected_symbol
        assert any(item.evidence_id == "S1" for item in result.evidence)


@pytest.mark.parametrize(
    "task_id, relative_file, symbol",
    [
        ("display-name-none", "src/profile.py", "display_name"),
        ("shipping-member-rule", "src/shipping.py", "shipping_fee"),
        ("setting-key-casefold", "src/key_utils.py", "normalize_key"),
        ("asset-path-traversal", "src/path_utils.py", "asset_path"),
    ],
)
def test_new_tasks_run_from_mock_agents_to_hidden_acceptance(
    task_id: str, relative_file: str, symbol: str, tmp_path: Path
) -> None:
    task = next(task for task in TASKS if task.id == task_id)
    reference_text = (task.task_root / "reference" / relative_file).read_text(
        encoding="utf-8"
    )
    definition = next(
        node for node in ast.parse(reference_text).body
        if isinstance(node, ast.FunctionDef) and node.name == symbol
    )
    replacement = ast.get_source_segment(reference_text, definition)
    assert replacement is not None
    locator_reply = json.dumps({
        "status": "hypothesis",
        "summary": "The observed behavior violates the task requirement.",
        "root_cause": "The target function needs a focused correction.",
        "target_file": relative_file,
        "target_symbol": symbol,
        "confidence": 0.7,
        "evidence_ids": ["F1", "S1"],
        "next_step": "Replace the target function.",
    })
    repair_reply = json.dumps({
        "status": "patch",
        "target_file": relative_file,
        "target_symbol": symbol,
        "replacement_source": replacement,
        "explanation": "Reference replacement for a deterministic integration test.",
        "evidence_ids": ["F1", "S1"],
    })

    run = run_repair_task(
        task, ScriptedModelClient(locator_reply), ScriptedModelClient(repair_reply)
    )

    assert run.validation_result is not None
    assert run.validation_result.public_tests_passed
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(run.validation_result.diff, encoding="utf-8")
    assert grade_saved_patch(task, patch_path).passed
