"""Tests for AST indexing and traceback-to-function localization."""

from pathlib import Path

from repofix.analysis import (
    SymbolKind,
    build_source_index,
    locate_test_failure,
    locate_traceback_frame,
)
from repofix.benchmarks.loader import load_tasks
from repofix.execution.models import TracebackFrame
from repofix.execution.runner import run_test_command
from repofix.workspace.manager import isolated_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "benchmarks" / "manifest.yaml"
PAGINATION_REPOSITORY = (
    PROJECT_ROOT
    / "benchmarks"
    / "tasks"
    / "pagination-zero-page-size"
    / "repo"
)


def test_indexes_pagination_function() -> None:
    source_index = build_source_index(PAGINATION_REPOSITORY)

    pagination_module = next(
        module
        for module in source_index.modules
        if module.relative_path == Path("src/pagination.py")
    )
    paginate = next(
        symbol
        for symbol in pagination_module.symbols
        if symbol.name == "paginate"
    )

    assert source_index.issues == ()
    assert pagination_module.name == "pagination"
    assert paginate.kind is SymbolKind.FUNCTION
    assert paginate.qualified_name == "paginate"
    assert paginate.start_line == 10
    assert paginate.end_line == 29
    assert tuple(parameter.name for parameter in paginate.parameters) == (
        "items",
        "page",
        "page_size",
    )
    assert "total_pages" in paginate.source_code


def test_locates_function_from_relative_traceback_path() -> None:
    source_index = build_source_index(PAGINATION_REPOSITORY)
    frame = TracebackFrame(
        file_path=Path("src/pagination.py"),
        line_number=23,
        function_name="paginate",
        source_line=(
            "total_pages = (len(items) + page_size - 1) // page_size"
        ),
    )

    location = locate_traceback_frame(source_index, frame)

    assert location is not None
    assert location.module.name == "pagination"
    assert location.symbol.name == "paginate"
    assert location.symbol.start_line <= 23 <= location.symbol.end_line
    assert location.exact_name_match is True


def test_prefers_innermost_nested_function(tmp_path: Path) -> None:
    source_file = tmp_path / "nested.py"
    source_file.write_text(
        "def outer():\n"
        "    def inner():\n"
        "        return 1 / 0\n"
        "    return inner()\n",
        encoding="utf-8",
    )
    source_index = build_source_index(tmp_path)
    frame = TracebackFrame(
        file_path=source_file,
        line_number=3,
        function_name="unknown_name",
        source_line="return 1 / 0",
    )

    location = locate_traceback_frame(source_index, frame)

    assert location is not None
    assert location.symbol.qualified_name == "outer.inner"
    assert location.exact_name_match is False


def test_locates_function_from_real_pytest_failure() -> None:
    task = load_tasks(MANIFEST_PATH)[0]

    with isolated_workspace(task) as workspace:
        result = run_test_command(
            working_path=workspace.repo_path,
            command=task.public_test_command,
            timeout_seconds=task.timeout_seconds,
        )
        source_index = build_source_index(workspace.repo_path)
        locations = locate_test_failure(
            source_index,
            result.testfailures[0],
        )

    paginate_location = next(
        location
        for location in locations
        if location.symbol.name == "paginate"
    )
    assert paginate_location.module.relative_path == Path(
        "src/pagination.py"
    )
    assert paginate_location.frame.line_number == 23
    assert paginate_location.exact_name_match is True
