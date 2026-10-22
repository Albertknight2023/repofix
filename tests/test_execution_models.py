from pathlib import Path

from repofix.execution.models import (
    TestFailure as Failure,
    TestRunResult as RunResult,
    TestRunStatus as RunStatus,
    TracebackFrame,
)


def test_creates_failed_test_result() -> None:
    frame = TracebackFrame(
        file_path=Path("src/pagination.py"),
        line_number=20,
        function_name="paginate",
        source_line="total_pages = 10 // page_size",
    )

    failure = Failure(
        test_id="tests/test_pagination.py::test_zero_page_size",
        phase="call",
        exception_type="ZeroDivisionError",
        message="integer division or modulo by zero",
        traceback_frames=(frame,),
    )

    result = RunResult(
        status=RunStatus.FAILED,
        command=("python", "-m", "pytest", "tests", "-q"),
        working_path=Path("workspace/repo"),
        exit_code=1,
        stdout="1 failed",
        stderr="",
        duration_seconds=0.5,
        testfailures=(failure,),
    )

    assert result.status is RunStatus.FAILED
    assert result.passed is False
    assert result.timed_out is False

    assert len(result.testfailures) == 1
    assert result.testfailures[0].exception_type == "ZeroDivisionError"
    assert result.testfailures[0].traceback_frames[0].line_number == 20
