"""Tests for executing commands in isolated workspaces."""

from pathlib import Path

from repofix.benchmarks.loader import load_tasks
from repofix.execution.models import TestRunStatus as RunStatus
from repofix.execution.runner import run_test_command
from repofix.workspace.manager import isolated_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "benchmarks" / "manifest.yaml"


def test_reports_passing_test_run() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    command = (
        "python -m pytest "
        "tests/test_pagination.py::test_returns_first_page -q"
    )

    with isolated_workspace(task) as workspace:
        result = run_test_command(
            working_path=workspace.repo_path,
            command=command,
            timeout_seconds=task.timeout_seconds,
        )

    assert result.status is RunStatus.PASSED
    assert result.passed is True
    assert result.timed_out is False
    assert result.exit_code == 0
    assert "passed" in result.stdout.lower()
    assert result.testfailures == ()


def test_reports_failing_test_run() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    with isolated_workspace(task) as workspace:
        result = run_test_command(
            working_path=workspace.repo_path,
            command=task.public_test_command,
            timeout_seconds=task.timeout_seconds,
        )

    assert result.status is RunStatus.FAILED
    assert result.passed is False
    assert result.timed_out is False
    assert result.exit_code == 1
    assert "failed" in result.stdout.lower()

    assert len(result.testfailures) == 1
    failure = result.testfailures[0]

    assert failure.test_id.endswith(
        "::test_rejects_zero_page_size"
    )
    assert failure.phase == "call"
    assert failure.exception_type == "ZeroDivisionError"
    assert "division" in failure.message.lower()

    assert any(
        frame.file_path.name == "pagination.py"
        and frame.function_name == "paginate"
        and frame.line_number == 23
        for frame in failure.traceback_frames
    )


def test_reports_timeout() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    with isolated_workspace(task) as workspace:
        result = run_test_command(
            working_path=workspace.repo_path,
            command='python -c "import time; time.sleep(2)"',
            timeout_seconds=0.05,
        )

    assert result.status is RunStatus.TIMEOUT
    assert result.passed is False
    assert result.timed_out is True
    assert result.exit_code is None
    assert result.testfailures == ()
