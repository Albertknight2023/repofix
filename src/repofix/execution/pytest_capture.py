"""Pytest plugin for capturing structured test-failure evidence.

The plugin is loaded in the benchmark's pytest subprocess. It records failure
details through pytest hooks and writes one JSON report for the parent RepoFix
process. The report path is supplied through an environment variable so that
the target repository never needs to know where RepoFix stores its artifacts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


REPORT_PATH_ENVIRONMENT_VARIABLE = "REPOFIX_PYTEST_REPORT_PATH"

_failures: list[dict[str, Any]] = []


def _source_line(traceback_entry: Any) -> str | None:
    """Return the source statement for one traceback entry when available."""

    try:
        statement = traceback_entry.statement
    except Exception:
        return None

    text = str(statement).strip()
    return text or None


def _traceback_frames(excinfo: Any) -> list[dict[str, Any]]:
    """Convert pytest traceback entries into JSON-serializable dictionaries."""

    frames: list[dict[str, Any]] = []

    for entry in excinfo.traceback:
        frames.append(
            {
                "file_path": str(Path(str(entry.path)).resolve()),
                # pytest traceback entry line numbers are zero-based.
                "line_number": int(entry.lineno) + 1,
                "function_name": str(entry.name),
                "source_line": _source_line(entry),
            }
        )

    return frames


def pytest_sessionstart(session: Any) -> None:
    """Clear data in case pytest reuses this plugin in one Python process."""

    del session
    _failures.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):
    """Capture setup, call, or teardown failures after pytest classifies them."""

    outcome = yield
    report = outcome.get_result()

    if not report.failed or call.excinfo is None:
        return

    _failures.append(
        {
            "test_id": item.nodeid,
            "phase": call.when,
            "exception_type": call.excinfo.typename,
            "message": str(call.excinfo.value),
            "traceback_frames": _traceback_frames(call.excinfo),
        }
    )


def pytest_collectreport(report: Any) -> None:
    """Capture import and collection failures that have no test-call object."""

    if not report.failed:
        return

    _failures.append(
        {
            "test_id": report.nodeid,
            "phase": "collection",
            "exception_type": "CollectionError",
            "message": report.longreprtext,
            "traceback_frames": [],
        }
    )


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """Write the captured failures after pytest completes normally."""

    del session
    raw_report_path = os.environ.get(REPORT_PATH_ENVIRONMENT_VARIABLE)

    if not raw_report_path:
        return

    report_path = Path(raw_report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 1,
        "exit_status": int(exitstatus),
        "failures": _failures,
    }

    temporary_path = report_path.with_name(f"{report_path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(report_path)
