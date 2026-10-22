"""Multi-candidate scheduling is reproducible without paid model calls."""

import json
from dataclasses import replace
from pathlib import Path

from repofix.agents.repair import RepairResult
from repofix.agents.workflow import CandidateAttempt, _rank, candidate_comparison, run_repair_task
from repofix.benchmarks.loader import load_task
from repofix.benchmarks.models import TaskSpec
from repofix.cli import main
from repofix.execution.runner import run_test_command
from repofix.execution.models import TestRunResult as RunResult, TestRunStatus as RunStatus
from repofix.llm import ScriptedModelClient, ScriptedSequenceClient
from repofix.repair import ValidationResult
from repofix.repair.reviewer import PatchReview, ReviewFinding
from repofix.reporting import save_repair_run


ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / "benchmarks/tasks/pagination-zero-page-size/task.yaml"
LOCATOR_PATH = ROOT / "examples/pagination-locator-response.json"
REPAIR_PATH = ROOT / "examples/pagination-repair-response.json"


def _answers() -> tuple[str, str]:
    good = json.loads(REPAIR_PATH.read_text(encoding="utf-8"))
    bad = dict(good)
    bad["replacement_source"] = good["replacement_source"].replace(
        "if page_size <= 0:", "if page_size == -1:"
    )
    bad["explanation"] = "Only handle minus one; intentionally misses zero."
    return json.dumps(bad), json.dumps(good)


def _locator() -> ScriptedModelClient:
    return ScriptedModelClient(LOCATOR_PATH.read_text(encoding="utf-8"))


def test_dynamic_retry_uses_public_feedback_and_selects_second(tmp_path: Path) -> None:
    task = load_task(TASK_PATH)
    original = (task.repo_path / "src/pagination.py").read_bytes()
    repair = ScriptedSequenceClient(_answers())
    locator = _locator()

    run = run_repair_task(task, locator, repair, workflow_mode="dynamic")
    saved = save_repair_run(task=task, run=run, output_root=tmp_path)
    report = json.loads(saved.report_path.read_text(encoding="utf-8"))

    assert [item.validation_result.status for item in run.attempts] == [
        "public_tests_failed", "public_tests_passed"
    ]
    assert run.selected_attempt == 2
    assert run.validation_result is not None and run.validation_result.public_tests_passed
    assert run.model_calls == 4
    assert len(locator.calls) == 2
    assert len(run.locator_attempts) == 2
    assert report["candidate_selection"]["selected_attempt"] == 2
    assert [event["action"] for event in report["schedule"]] == [
        "locate", "generate_candidate", "relocalize",
        "generate_second_candidate", "select_candidate_2"
    ]
    assert "same public tests" in report["schedule"][3]["reason"]
    assert report["model_calls"] == 4
    assert len(report["diagnostic_rounds"]) == 2
    assert (saved.run_directory / "candidate_01.patch").is_file()
    assert (saved.run_directory / "candidate_02.patch").is_file()
    assert saved.patch_path is not None
    assert saved.patch_path.read_bytes() == (saved.run_directory / "candidate_02.patch").read_bytes()
    assert saved.markdown_path is not None and saved.markdown_path.is_file()
    assert "隐藏测试未在修复阶段运行" in saved.markdown_path.read_text(encoding="utf-8")
    known = {item["id"] for item in report["evidence"]}
    assert all(set(claim["evidence_refs"]) <= known for claim in report["evidence_chain"])
    assert {claim["kind"] for claim in report["evidence_chain"]} >= {
        "observation", "model_hypothesis", "proposed_change", "test_observation"
    }
    assert (task.repo_path / "src/pagination.py").read_bytes() == original
    second_prompt = json.loads(repair.calls[1][1].content)
    assert second_prompt["repair_strategy"] == "edge-case-complete"
    assert second_prompt["public_validation_feedback"]["validation_status"] == "public_tests_failed"
    assert second_prompt["public_validation_feedback"]["public_failures"]
    assert "grader_tests" not in json.dumps(second_prompt)
    assert "solution.patch" not in json.dumps(second_prompt)
    revised_prompt = json.loads(locator.calls[1][1].content)
    assert "previous_submission_feedback" in revised_prompt
    assert revised_prompt["eligible_targets"]
    assert "grader_tests" not in json.dumps(revised_prompt)
    memory = second_prompt["structured_memory"]
    assert memory["scope"] == "single_run"
    assert memory["project"]["function_count"] >= 1
    assert len(memory["iterations"]) == 1
    assert memory["iterations"][0]["validation_status"] == "public_tests_failed"
    assert len(report["structured_memory"]["iterations"]) == 2
    assert "grader_tests" not in json.dumps(report["structured_memory"])


def test_dynamic_stops_after_first_clean_pass() -> None:
    good = REPAIR_PATH.read_text(encoding="utf-8")
    repair = ScriptedSequenceClient((good,))
    run = run_repair_task(load_task(TASK_PATH), _locator(), repair, workflow_mode="dynamic")

    assert len(repair.calls) == len(run.attempts) == 1
    assert run.schedule[2].action == "stop_generation"
    assert run.model_calls == 2
    assert run.selected_attempt == 1
    assert run.validation_result is not None and run.validation_result.public_tests_passed
    assert candidate_comparison(run.attempts, run.workflow_mode)["status"] == "not_triggered"


def test_dynamic_relocalizes_when_failed_patch_leaves_only_test_frames(
    tmp_path: Path,
) -> None:
    root = tmp_path / "expected-exception"
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (root / "grader_tests").mkdir()
    (repo / "logic.py").write_text(
        "def length(value):\n"
        "    if value is None:\n"
        "        return value.missing\n"
        "    return len(value)\n",
        encoding="utf-8",
    )
    (repo / "tests/test_logic.py").write_text(
        "import pytest\n"
        "from logic import length\n\n"
        "def test_unknown_length():\n"
        "    with pytest.raises(TypeError):\n"
        "        length(None)\n",
        encoding="utf-8",
    )
    task = TaskSpec(
        id="expected-exception", title="Preserve the expected exception",
        language="python", test_framework="pytest",
        issue="length(None) must raise TypeError, not AttributeError or return zero.",
        task_root=root, repo_path=repo, grader_test_path=root / "grader_tests",
        public_test_command="python -m pytest tests/test_logic.py -q",
        full_test_command="python -m pytest tests/test_logic.py ../grader_tests -q",
        timeout_seconds=30, allowed_edit_paths=("logic.py",),
        protected_paths=("../grader_tests",),
        expected_initial_status="FAILED", expected_reference_status="PASSED",
    )
    diagnosis = json.dumps({
        "status": "hypothesis", "summary": "Inspect length",
        "root_cause": "None currently triggers AttributeError",
        "target_file": "logic.py", "target_symbol": "length",
        "confidence": 0.8, "evidence_ids": ["F1", "S1"],
        "next_step": "Check the required exception semantics.",
    })
    locator = ScriptedSequenceClient((diagnosis, diagnosis))

    def patch(body: str) -> str:
        return json.dumps({
            "status": "patch", "target_file": "logic.py",
            "target_symbol": "length", "replacement_source": body,
            "explanation": "Revise the None branch.",
            "evidence_ids": ["F1", "S1"],
        })

    repair = ScriptedSequenceClient((
        patch("def length(value):\n    if value is None:\n        return 0\n    return len(value)\n"),
        patch("def length(value):\n    if value is None:\n        raise TypeError('unknown length')\n    return len(value)\n"),
    ))

    run = run_repair_task(task, locator, repair, workflow_mode="dynamic")

    assert len(locator.calls) == len(repair.calls) == 2
    assert run.model_calls == 4
    assert [attempt.validation_result.status for attempt in run.attempts] == [
        "public_tests_failed", "public_tests_passed",
    ]
    assert run.selected_attempt == 2
    revised = json.loads(locator.calls[1][1].content)
    assert revised["eligible_targets"] == [{
        "source_evidence_id": "S1", "target_file": "logic.py",
        "target_symbol": "length",
    }]
    assert any("return 0" in item["content"] for item in revised["evidence"])
    assert any("DID NOT RAISE" in item["content"] for item in revised["evidence"])
    assert (repo / "logic.py").read_text(encoding="utf-8").count("value.missing") == 1


def _two_function_task(tmp_path: Path) -> TaskSpec:
    root = tmp_path / "two-function-task"
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (root / "grader_tests").mkdir()
    (repo / "logic.py").write_text(
        "def count(items):\n"
        "    return len(items)\n\n"
        "def render(items):\n"
        "    return count(items)\n",
        encoding="utf-8",
    )
    (repo / "tests/test_logic.py").write_text(
        "from logic import render\n\n"
        "def test_none():\n"
        "    assert render(None) == 0\n",
        encoding="utf-8",
    )
    return TaskSpec(
        id="two-function-relocalize", title="Revisit a wrong target",
        language="python", test_framework="pytest",
        issue="render(None) should return zero without raising.",
        task_root=root, repo_path=repo, grader_test_path=root / "grader_tests",
        public_test_command="python -m pytest tests/test_logic.py -q",
        full_test_command="python -m pytest tests/test_logic.py ../grader_tests -q",
        timeout_seconds=30, allowed_edit_paths=("logic.py",),
        protected_paths=("../grader_tests",),
        expected_initial_status="FAILED", expected_reference_status="PASSED",
    )


def _locator_answer(symbol: str, source_id: str) -> str:
    return json.dumps({
        "status": "hypothesis", "summary": f"Inspect {symbol}",
        "root_cause": f"{symbol} mishandles None", "target_file": "logic.py",
        "target_symbol": symbol, "confidence": 0.8,
        "evidence_ids": ["F1", source_id], "next_step": f"Fix {symbol}",
    })


def _repair_answer(symbol: str, replacement: str, source_id: str) -> str:
    return json.dumps({
        "status": "patch", "target_file": "logic.py", "target_symbol": symbol,
        "replacement_source": replacement, "explanation": f"Update {symbol}",
        "evidence_ids": ["F1", source_id],
    })


def test_dynamic_relocalization_can_change_target_function(tmp_path: Path) -> None:
    task = _two_function_task(tmp_path)
    locator = ScriptedSequenceClient((
        _locator_answer("render", "S1"), _locator_answer("count", "S2"),
    ))
    repair = ScriptedSequenceClient((
        _repair_answer(
            "render", "def render(items):\n"
            "    if items is None:\n"
            "        return count(items)\n"
            "    return count(items)\n", "S1",
        ),
        _repair_answer(
            "count", "def count(items):\n"
            "    if items is None:\n"
            "        return 0\n"
            "    return len(items)\n", "S2",
        ),
    ))

    run = run_repair_task(task, locator, repair, workflow_mode="dynamic")
    report_path = save_repair_run(task=task, run=run, output_root=tmp_path / "runs").report_path
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert [item.validation_result.status for item in run.attempts] == [
        "public_tests_failed", "public_tests_passed",
    ]
    assert [item.hypothesis.target_symbol for item in run.locator_attempts] == [
        "render", "count",
    ]
    assert run.locator_result.hypothesis.target_symbol == "count"
    assert run.attempts[1].repair_result.candidate.target_symbol == "count"
    assert run.selected_attempt == 2
    assert run.model_calls == 4
    assert [item["target_symbol"] for item in report["diagnostic_rounds"]] == [
        "render", "count",
    ]
    assert report["diagnosis"]["hypothesis"]["target_symbol"] == "count"
    assert report["structured_memory"]["bug"]["relocalized_after_attempt"] == 1
    revised_locator_payload = json.loads(locator.calls[1][1].content)
    assert any("return count(items)" in item["content"] for item in revised_locator_payload["evidence"])
    second_repair_payload = json.loads(repair.calls[1][1].content)
    assert second_repair_payload["hypothesis"]["target_symbol"] == "count"
    assert "grader_tests" not in json.dumps(second_repair_payload)
    assert (task.repo_path / "logic.py").read_text(encoding="utf-8").endswith(
        "    return count(items)\n"
    )


def test_dynamic_stops_when_relocalization_has_no_grounded_target(tmp_path: Path) -> None:
    task = _two_function_task(tmp_path)
    locator = ScriptedSequenceClient((
        _locator_answer("render", "S1"),
        '{"status":"insufficient_evidence","explanation":"Cannot justify a target."}',
    ))
    repair = ScriptedSequenceClient((_repair_answer(
        "render", "def render(items):\n"
        "    if items is None:\n"
        "        return count(items)\n"
        "    return count(items)\n", "S1",
    ),))

    run = run_repair_task(task, locator, repair, workflow_mode="dynamic")

    assert len(run.attempts) == 1
    assert len(locator.calls) == 2
    assert len(repair.calls) == 1
    assert run.model_calls == 3
    assert run.schedule[-2].action == "stop_generation"
    assert run.selected_attempt == 1


def test_dynamic_does_not_target_function_only_added_by_failed_patch(tmp_path: Path) -> None:
    task = _two_function_task(tmp_path)
    locator = ScriptedSequenceClient((
        _locator_answer("render", "S1"),
        _locator_answer("render.interim", "S2"),
    ))
    repair = ScriptedSequenceClient((_repair_answer(
        "render", "def render(items):\n"
        "    def interim(value):\n"
        "        return len(value)\n"
        "    return interim(items)\n", "S1",
    ),))

    run = run_repair_task(task, locator, repair, workflow_mode="dynamic")

    assert len(run.attempts) == 1
    assert run.model_calls == 3
    assert run.schedule[-2].evidence == "relocalize:target_unavailable"
    assert run.selected_attempt == 1


def test_competitive_retries_duplicate_until_two_different_diffs() -> None:
    good = REPAIR_PATH.read_text(encoding="utf-8")
    bad, _ = _answers()
    repair = ScriptedSequenceClient((good, good, bad))
    calls = []

    def runner(working_path: Path, command: str, timeout_seconds: float):
        calls.append(working_path)
        return run_test_command(working_path, command, timeout_seconds)

    run = run_repair_task(
        load_task(TASK_PATH), _locator(), repair,
        workflow_mode="competitive", test_runner=runner,
    )
    assert len(repair.calls) == len(run.attempts) == 3
    assert run.attempts[1].validation_result is not None
    assert run.attempts[1].validation_result.status == "duplicate"
    assert run.selected_attempt == 1
    assert len(calls) == 3  # Initial reproduction and two different candidate tests.
    assert candidate_comparison(run.attempts, run.workflow_mode)["status"] == "compared"
    second_prompt = json.loads(repair.calls[1][1].content)
    assert second_prompt["repair_strategy"] == "edge-case-complete"
    assert "public_validation_feedback" not in second_prompt
    assert second_prompt["structured_memory"]["iterations"] == []
    third_prompt = json.loads(repair.calls[2][1].content)
    assert third_prompt["repair_strategy"] == "alternative-control-flow"
    assert "public_validation_feedback" not in third_prompt
    assert third_prompt["structured_memory"]["iterations"] == []


def test_competitive_records_incomplete_comparison_after_bounded_duplicates() -> None:
    good = REPAIR_PATH.read_text(encoding="utf-8")
    repair = ScriptedSequenceClient((good, good, good, good))

    run = run_repair_task(
        load_task(TASK_PATH), _locator(), repair, workflow_mode="competitive"
    )

    assert len(repair.calls) == len(run.attempts) == 4
    assert run.model_calls == 5
    assert [attempt.validation_result.status for attempt in run.attempts] == [
        "public_tests_passed", "duplicate", "duplicate", "duplicate",
    ]
    assert run.schedule[-2].action == "competition_incomplete"
    assert candidate_comparison(run.attempts, run.workflow_mode) == {
        "status": "insufficient_distinct_valid_candidates",
        "eligible_count": 1,
        "ranking_rule": (
            "public pass > public failure > timeout/error > rejected > duplicate/no patch; "
            "then fewer failures, fewer review warnings, smaller change, earlier candidate"
        ),
    }


def test_competitive_retries_same_diff_from_different_edit_modes() -> None:
    good = json.loads(REPAIR_PATH.read_text(encoding="utf-8"))
    snippet = {
        "status": "patch",
        "edit_kind": "snippet",
        "target_file": good["target_file"],
        "target_symbol": good["target_symbol"],
        "old_snippet": "    # Calculating the page count before validating page_size causes the target\n",
        "new_snippet": (
            "    if page_size <= 0:\n"
            "        raise ValueError(\"page_size must be greater than 0\")\n\n"
            "    # Calculating the page count before validating page_size causes the target\n"
        ),
        "explanation": "Add the same check using snippet mode.",
        "evidence_ids": good["evidence_ids"],
    }
    bad, _ = _answers()
    repair = ScriptedSequenceClient((json.dumps(good), json.dumps(snippet), bad))

    run = run_repair_task(
        load_task(TASK_PATH), _locator(), repair, workflow_mode="competitive"
    )

    assert len(run.attempts) == 3
    assert run.attempts[0].fingerprint != run.attempts[1].fingerprint
    assert run.attempts[1].validation_result.status == "duplicate"
    assert run.attempts[0].validation_result.diff == run.attempts[1].validation_result.diff
    assert candidate_comparison(run.attempts, run.workflow_mode)["status"] == "compared"


def test_competitive_does_not_count_comment_only_variation_as_new_patch() -> None:
    good = json.loads(REPAIR_PATH.read_text(encoding="utf-8"))
    cosmetic = dict(good)
    cosmetic["replacement_source"] = good["replacement_source"].replace(
        "# Calculating the page count before validating page_size causes the target",
        "# Page count is computed after input validation",
    )
    bad, _ = _answers()
    repair = ScriptedSequenceClient((json.dumps(good), json.dumps(cosmetic), bad))

    run = run_repair_task(
        load_task(TASK_PATH), _locator(), repair, workflow_mode="competitive"
    )

    assert len(run.attempts) == 3
    assert run.attempts[0].validation_result.diff != run.attempts[1].validation_result.diff
    assert run.attempts[1].validation_result.status == "duplicate"
    assert candidate_comparison(run.attempts, run.workflow_mode)["status"] == "compared"


def test_competitive_independently_compares_distinct_patches(tmp_path: Path) -> None:
    repair = ScriptedSequenceClient(_answers())
    task = load_task(TASK_PATH)

    run = run_repair_task(task, _locator(), repair, workflow_mode="competitive")
    saved = save_repair_run(task=task, run=run, output_root=tmp_path)
    report = json.loads(saved.report_path.read_text(encoding="utf-8"))

    assert [attempt.validation_result.status for attempt in run.attempts] == [
        "public_tests_failed", "public_tests_passed",
    ]
    assert run.selected_attempt == 2
    assert len({attempt.fingerprint for attempt in run.attempts}) == 2
    assert run.model_calls == 3  # One locator and two independent repairs.
    assert report["candidate_selection"]["comparison"]["status"] == "compared"
    assert report["candidate_selection"]["comparison"]["eligible_count"] == 2
    assert report["candidate_selection"]["attempts"][1]["rank_key"][0] > (
        report["candidate_selection"]["attempts"][0]["rank_key"][0]
    )
    assert (saved.run_directory / "candidate_01.patch").is_file()
    assert (saved.run_directory / "candidate_02.patch").is_file()
    second_prompt = json.loads(repair.calls[1][1].content)
    assert "public_validation_feedback" not in second_prompt
    assert second_prompt["structured_memory"]["iterations"] == []
    assert "grader_tests" not in json.dumps(second_prompt)


def test_failed_second_candidate_does_not_displace_valid_first() -> None:
    good = REPAIR_PATH.read_text(encoding="utf-8")
    bad, _ = _answers()
    no_patch = '{"status":"no_patch","explanation":"No safe alternative."}'
    repair = ScriptedSequenceClient((good, no_patch, bad))
    run = run_repair_task(
        load_task(TASK_PATH), _locator(), repair, workflow_mode="competitive"
    )
    assert len(run.attempts) == 3
    assert run.attempts[1].repair_result.status == "no_patch"
    assert run.selected_attempt == 1
    assert run.validation_result is not None and run.validation_result.public_tests_passed
    assert candidate_comparison(run.attempts, run.workflow_mode)["status"] == "compared"


def test_cli_accepts_offline_answer_sequence(tmp_path: Path, capsys) -> None:
    sequence = ROOT / "examples/pagination-repair-sequence.json"
    status = main([
        "repair", "--task", str(TASK_PATH), "--runner", "host",
        "--provider", "mock", "--workflow", "dynamic",
        "--mock-response", str(LOCATOR_PATH),
        "--mock-repair-response", str(sequence),
        "--output-root", str(tmp_path / "runs"),
    ])
    output = json.loads(capsys.readouterr().out)
    assert status == 0
    assert output["candidate_count"] == 2
    assert output["selected_attempt"] == 2
    assert output["model_calls"] == 4


def test_cli_competitive_reports_independent_candidate_comparison(
    tmp_path: Path, capsys,
) -> None:
    status = main([
        "repair", "--task", str(TASK_PATH), "--runner", "host",
        "--provider", "mock", "--workflow", "competitive",
        "--mock-response", str(LOCATOR_PATH),
        "--mock-repair-response", str(ROOT / "examples/pagination-repair-sequence.json"),
        "--output-root", str(tmp_path / "runs"),
    ])
    output = json.loads(capsys.readouterr().out)
    report = json.loads(Path(output["report_path"]).read_text(encoding="utf-8"))

    assert status == 0
    assert output["candidate_count"] == 2
    assert output["selected_attempt"] == 2
    assert output["model_calls"] == 3
    assert report["candidate_selection"]["comparison"]["status"] == "compared"
    assert len(report["candidate_selection"]["attempts"]) == 2


def test_selection_prefers_clean_and_small_publicly_passing_patch() -> None:
    passed = RunResult(
        status=RunStatus.PASSED, command=(), working_path=Path("."),
        exit_code=0, stdout="", stderr="", duration_seconds=0,
    )
    proposal = RepairResult("patch", None, "", "mock", None, None)
    warning = ReviewFinding("large_change", "warning", "large")
    risky = CandidateAttempt(1, "wide", proposal, ValidationResult(
        "public_tests_passed", "", passed, "",
        PatchReview("warnings", 90, (warning,)),
    ))
    clean_large = CandidateAttempt(2, "clean", proposal, ValidationResult(
        "public_tests_passed", "", passed, "",
        PatchReview("passed", 10, ()),
    ))
    clean_small = replace(clean_large, number=3, validation_result=replace(
        clean_large.validation_result, review=PatchReview("passed", 2, ()),
    ))
    assert max((risky, clean_large, clean_small), key=_rank) is clean_small
    earlier = replace(clean_small, number=2)
    assert max((earlier, clean_small), key=_rank) is earlier


def test_selection_does_not_treat_timeout_as_zero_failures() -> None:
    failed = RunResult(
        status=RunStatus.FAILED, command=(), working_path=Path("."),
        exit_code=1, stdout="", stderr="", duration_seconds=0,
    )
    timed_out = replace(failed, status=RunStatus.TIMEOUT, exit_code=None)
    proposal = RepairResult("patch", None, "", "mock", None, None)
    actual_failure = CandidateAttempt(1, "tested", proposal, ValidationResult(
        "public_tests_failed", "diff", failed, "assertion failed",
    ))
    timeout = CandidateAttempt(2, "timed-out", proposal, ValidationResult(
        "public_tests_failed", "diff", timed_out, "timeout",
    ))
    assert max((actual_failure, timeout), key=_rank) is actual_failure
