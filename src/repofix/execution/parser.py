"""Convert raw pytest failure reports into RepoFix execution models."""

import json
from pathlib import Path
from typing import Any

from repofix.execution.models import TestFailure, TracebackFrame


def _required_string(data: dict[str, Any], field: str) -> str:
    value = data.get(field)

    if not isinstance(value, str) or not value:
        raise ValueError(f"pytest report field must be a non-empty string: {field}")

    return value


def _parse_frame(data: Any) -> TracebackFrame:
    if not isinstance(data, dict):
        raise ValueError("pytest traceback frame must be a mapping")

    line_number = data.get("line_number")
    if not isinstance(line_number, int) or line_number < 1:
        raise ValueError("pytest traceback line_number must be a positive integer")

    source_line = data.get("source_line")
    if source_line is not None and not isinstance(source_line, str):
        raise ValueError("pytest traceback source_line must be a string or null")

    return TracebackFrame(
        file_path=Path(_required_string(data, "file_path")),
        line_number=line_number,
        function_name=_required_string(data, "function_name"),
        source_line=source_line,
    )


def _parse_failure(data: Any) -> TestFailure:
    if not isinstance(data, dict):
        raise ValueError("pytest failure must be a mapping")

    raw_frames = data.get("traceback_frames")
    if not isinstance(raw_frames, list):
        raise ValueError("pytest failure traceback_frames must be a list")

    return TestFailure(
        test_id=_required_string(data, "test_id"),
        phase=_required_string(data, "phase"),
        exception_type=_required_string(data, "exception_type"),
        message=_required_string(data, "message"),
        traceback_frames=tuple(_parse_frame(frame) for frame in raw_frames),
    )


def parse_pytest_report(report_path: Path) -> tuple[TestFailure, ...]:
    """Read one RepoFix pytest JSON report."""

    report_path = report_path.resolve()

    if not report_path.is_file():
        raise FileNotFoundError(f"pytest report does not exist: {report_path}")

    data = json.loads(report_path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("pytest report root must be a mapping")

    if data.get("schema_version") != 1:
        raise ValueError("unsupported pytest report schema version")

    raw_failures = data.get("failures")
    if not isinstance(raw_failures, list):
        raise ValueError("pytest report failures must be a list")

    return tuple(_parse_failure(failure) for failure in raw_failures)
