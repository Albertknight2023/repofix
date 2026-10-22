"""The offline grader must not reveal hidden test bodies to Agents."""

import difflib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from repofix.benchmarks.loader import load_task
from repofix.cli import main
from repofix.grader import grade_saved_patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = (
    PROJECT_ROOT / "benchmarks" / "tasks" / "pagination-zero-page-size" / "task.yaml"
)


def _save_diff(
    tmp_path: Path,
    relative_file: str,
    old_text: str,
    new_text: str,
) -> Path:
    diff = "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{relative_file}",
        tofile=f"b/{relative_file}",
    ))
    patch_path = tmp_path / "candidate.patch"
    patch_path.write_text(diff, encoding="utf-8", newline="\n")
    return patch_path


def test_reference_patch_passes_public_and_hidden_tests() -> None:
    task = load_task(TASK_PATH)
    original_path = task.repo_path / "src" / "pagination.py"
    original = original_path.read_bytes()

    result = grade_saved_patch(task, task.task_root / "solution.patch")

    assert result.passed
    assert result.public_test_status.value == "passed"
    assert result.hidden_test_status.value == "passed"
    assert original_path.read_bytes() == original


def test_public_only_fix_fails_hidden_acceptance_without_leaking_details(
    tmp_path: Path, capsys
) -> None:
    task = load_task(TASK_PATH)
    old = (task.repo_path / "src" / "pagination.py").read_text(encoding="utf-8")
    new = old.replace(
        "    total_pages = (len(items) + page_size - 1) // page_size\n",
        "    if page_size == 0:\n"
        "        raise ValueError('page_size must not be zero')\n"
        "    total_pages = (len(items) + page_size - 1) // page_size\n",
        1,
    )
    patch = _save_diff(tmp_path, "src/pagination.py", old, new)

    exit_code = main([
        "grade", "--task", str(TASK_PATH), "--patch", str(patch),
        "--runner", "host",
    ])

    assert exit_code == 2
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "failed"
    assert result["public_test_status"] == "passed"
    assert result["hidden_test_status"] == "failed"
    assert "test_rejects_negative_page_size" not in output
    assert "grader_tests" not in output
    assert "page_size=-1" not in output


def test_grader_rejects_attempt_to_modify_public_tests(tmp_path: Path) -> None:
    task = load_task(TASK_PATH)
    old = (task.repo_path / "tests" / "test_pagination.py").read_text(
        encoding="utf-8"
    )
    patch = _save_diff(
        tmp_path, "tests/test_pagination.py", old, old + "\n# changed\n"
    )

    result = grade_saved_patch(task, patch)

    assert result.status == "invalid_patch"
    assert result.public_test_status is None
    assert result.hidden_test_status is None
    assert "cannot modify tests" in result.reason


def test_grader_rejects_patch_against_wrong_baseline(tmp_path: Path) -> None:
    task = load_task(TASK_PATH)
    patch = _save_diff(
        tmp_path, "src/pagination.py",
        "def paginate():\n    return 1\n",
        "def paginate():\n    return 2\n",
    )

    result = grade_saved_patch(task, patch)

    assert result.status == "invalid_patch"
    assert result.hidden_test_status is None
    assert "does not apply cleanly" in result.reason


def test_grader_rejects_multifile_patch(tmp_path: Path) -> None:
    task = load_task(TASK_PATH)
    old = (task.repo_path / "src" / "pagination.py").read_text(encoding="utf-8")
    patch = _save_diff(
        tmp_path, "src/pagination.py", old, old.replace("return []", "return list()", 1)
    )
    patch.write_text(
        patch.read_text(encoding="utf-8")
        + "--- a/tests/test_pagination.py\n"
        + "+++ b/tests/test_pagination.py\n"
        + "@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )

    result = grade_saved_patch(task, patch)

    assert result.status == "invalid_patch"
    assert result.hidden_test_status is None
    assert "second file header" in result.reason


def test_grader_refuses_command_that_omits_hidden_tests() -> None:
    task = load_task(TASK_PATH)
    unsafe_task = replace(task, full_test_command=task.public_test_command)

    with pytest.raises(ValueError, match="grader_tests"):
        grade_saved_patch(unsafe_task, task.task_root / "solution.patch")
