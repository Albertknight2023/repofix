"""Patch-policy rules must be reproducible and enforced before any tests run."""

import difflib
from pathlib import Path

import pytest

from repofix.agents.repair import PatchCandidate
from repofix.analysis import build_source_index
from repofix.benchmarks.loader import load_task, load_tasks
from repofix.execution.models import TestRunResult as RunResult, TestRunStatus as RunStatus
from repofix.grader import grade_saved_patch
from repofix.repair import validate_candidate
from repofix.repair.reviewer import review_python_patch
from repofix.workspace.manager import isolated_workspace


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "benchmarks/tasks/pagination-zero-page-size/task.yaml"
REFERENCE_TASKS = (
    *load_tasks(ROOT / "benchmarks/manifest.yaml"),
    *load_tasks(ROOT / "benchmarks/real/manifest.yaml"),
)


def test_review_does_not_flag_preexisting_risks() -> None:
    before = "def f(x):\n    try:\n        return x\n    except Exception:\n        pass\n"
    after = before + "\ndef g(x):\n    return x + 1\n"

    review = review_python_patch(before, after)

    assert review.status == "passed"
    assert review.findings == ()


def test_review_blocks_new_test_skip_and_exception_swallow() -> None:
    before = "def f(x):\n    return x + 1\n"
    after = (
        "def f(x):\n"
        "    try:\n"
        "        pytest.skip('avoid test')\n"
        "    except Exception:\n"
        "        return None\n"
    )

    review = review_python_patch(before, after)

    assert review.status == "rejected"
    assert review.to_dict()["policy_version"] == "patch-review-v1"
    assert {finding.code for finding in review.findings} == {"test_skip", "swallowed_exception"}
    assert all(finding.severity == "block" for finding in review.findings)


def test_review_warns_about_constant_function_without_blocking() -> None:
    review = review_python_patch(
        "def f(x):\n    return x + 1\n",
        "def f(x):\n    return 42\n",
    )
    assert review.status == "warnings"
    assert review.findings[0].code == "constant_function"


def test_candidate_policy_rejects_before_public_tests() -> None:
    task = load_task(TASK)
    calls: list[str] = []

    def no_tests(*args, **kwargs):
        calls.append("called")
        raise AssertionError("policy rejection must precede public tests")

    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task, repo_path=workspace.repo_path, source_index=index,
            candidate=PatchCandidate(
                target_file=Path("src/pagination.py"), target_symbol="paginate",
                replacement_source=(
                    "def paginate(items: Sequence[T], page: int, page_size: int) -> list[T]:\n"
                    "    try:\n"
                    "        return list(items[:page_size])\n"
                    "    except Exception:\n"
                    "        pass\n"
                ),
                explanation="test", evidence_ids=("F1",),
            ),
            test_runner=no_tests,
        )

    assert result.status == "rejected"
    assert result.review is not None and result.review.status == "rejected"
    assert result.public_test_result is None
    assert not calls


def test_offline_grader_applies_same_policy_before_any_tests(tmp_path: Path) -> None:
    task = load_task(TASK)
    original = (task.repo_path / "src/pagination.py").read_text(encoding="utf-8")
    edited = original.replace(
        "    total_pages = (len(items) + page_size - 1) // page_size\n",
        "    try:\n"
        "        total_pages = (len(items) + page_size - 1) // page_size\n"
        "    except Exception:\n"
        "        return None\n",
        1,
    )
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), edited.splitlines(keepends=True),
        fromfile="a/src/pagination.py", tofile="b/src/pagination.py",
    ))
    patch = tmp_path / "candidate.patch"
    patch.write_text(diff, encoding="utf-8")

    def no_tests(*args, **kwargs):
        raise AssertionError("policy rejection must precede all tests")

    result = grade_saved_patch(task, patch, test_runner=no_tests)

    assert result.status == "rejected_patch"
    assert result.public_test_status is None and result.hidden_test_status is None
    assert result.review is not None and result.review.status == "rejected"


@pytest.mark.parametrize("task", REFERENCE_TASKS, ids=lambda task: task.id)
def test_known_reference_patches_are_not_blocked(task) -> None:
    def fake_pass(working_path, command, timeout_seconds):
        return RunResult(
            status=RunStatus.PASSED, command=("pytest",),
            working_path=working_path, exit_code=0,
            stdout="", stderr="", duration_seconds=0.0,
        )

    grade = grade_saved_patch(task, task.task_root / "solution.patch", test_runner=fake_pass)

    assert grade.status == "passed"
    assert grade.review is not None and grade.review.status != "rejected"
