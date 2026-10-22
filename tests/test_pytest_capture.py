"""Tests for parsing structured failure data captured from pytest."""

import json
from pathlib import Path

import pytest

from repofix.execution.parser import parse_pytest_report


def test_parses_captured_failure_report(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "exit_status": 1,
                "failures": [
                    {
                        "test_id": (
                            "tests/test_pagination.py"
                            "::test_rejects_zero_page_size"
                        ),
                        "phase": "call",
                        "exception_type": "ZeroDivisionError",
                        "message": "integer division or modulo by zero",
                        "traceback_frames": [
                            {
                                "file_path": "src/pagination.py",
                                "line_number": 23,
                                "function_name": "paginate",
                                "source_line": (
                                    "total_pages = "
                                    "(len(items) + page_size - 1) // page_size"
                                ),
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    failures = parse_pytest_report(report_path)

    assert len(failures) == 1
    assert failures[0].exception_type == "ZeroDivisionError"
    assert failures[0].traceback_frames[0].line_number == 23
    assert failures[0].traceback_frames[0].file_path == Path(
        "src/pagination.py"
    )


def test_rejects_unsupported_report_schema(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "schema_version": 999,
                "failures": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema version"):
        parse_pytest_report(report_path)
