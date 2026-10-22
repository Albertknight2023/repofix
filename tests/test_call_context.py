"""Verify static call relations and bounded context selection."""

from pathlib import Path

from repofix.agents.locator import build_diagnostic_evidence
from repofix.analysis import (
    build_call_graph,
    build_relevant_context,
    build_source_index,
)
from repofix.execution.models import TestFailure as Failure
from repofix.execution.models import TestRunResult as RunResult
from repofix.execution.models import TestRunStatus as RunStatus
from repofix.execution.models import TracebackFrame as Frame


def test_finds_cross_file_caller_without_guessing_dynamic_calls(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    helper_path = source_dir / "helper.py"
    helper_path.write_text(
        "def check(value):\n"
        "    return 10 / value\n",
        encoding="utf-8",
    )
    (source_dir / "service.py").write_text(
        "from helper import check\n"
        "\n"
        "def process():\n"
        "    return check(0)\n"
        "\n"
        "def dynamic(obj):\n"
        "    return getattr(obj, 'run')()\n",
        encoding="utf-8",
    )
    index = build_source_index(tmp_path)
    graph = build_call_graph(index)
    target = next(
        symbol for symbol in index.symbols
        if symbol.relative_path == Path("src/helper.py")
        and symbol.name == "check"
    )

    callers = graph.callers_of(target)
    assert len(callers) == 1
    assert callers[0].caller.qualified_name == "process"
    assert callers[0].call_line == 4
    assert any(
        edge.caller.name == "dynamic" and edge.callee is None
        for edge in graph.edges
    )

    failure = Failure(
        test_id="tests/test_service.py::test_process",
        phase="call",
        exception_type="ZeroDivisionError",
        message="division by zero",
        traceback_frames=(
            Frame(
                file_path=helper_path,
                line_number=2,
                function_name="check",
            ),
        ),
    )
    context = build_relevant_context(index, graph, failure)
    assert context is not None
    assert context.target == target
    assert len(context.related) == 1
    assert context.related[0].file_path == Path("src/service.py")
    assert context.related[0].relation == "caller"
    assert "return check(0)" in context.related[0].source_code


def test_context_respects_size_limit(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text(
        "def target():\n"
        "    raise ValueError('bad')\n"
        "\n"
        "def caller():\n"
        "    return target()\n",
        encoding="utf-8",
    )
    index = build_source_index(tmp_path)
    graph = build_call_graph(index)
    failure = Failure(
        test_id="tests/test_a.py::test_bad",
        phase="call",
        exception_type="ValueError",
        message="bad",
        traceback_frames=(
            Frame(
                file_path=tmp_path / "a.py",
                line_number=2,
                function_name="target",
            ),
        ),
    )

    context = build_relevant_context(
        index, graph, failure, max_related=2, max_characters=1
    )

    assert context is not None
    assert context.related == ()


def test_method_context_includes_initializer_writes_to_used_attributes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "progress.py"
    filler = "".join(f"        unused_{number} = {number}\n" for number in range(70))
    source.write_text(
        "class Progress:\n"
        "    def __init__(self, total=None):\n"
        + filler
        + "        self.total = total\n"
        "\n"
        "    def __len__(self):\n"
        "        return self.total + 1\n",
        encoding="utf-8",
    )
    index = build_source_index(tmp_path)
    graph = build_call_graph(index)
    method = next(symbol for symbol in index.symbols if symbol.name == "__len__")
    failure = Failure(
        test_id="tests/test_progress.py::test_length", phase="call",
        exception_type="TypeError", message="bad length",
        traceback_frames=(Frame(
            file_path=source, line_number=method.end_line,
            function_name="__len__",
        ),),
    )
    context = build_relevant_context(index, graph, failure)
    assert context is not None
    initializer = next(
        item for item in context.related
        if item.relation == "class_initializer"
    )
    assert initializer.symbol_name == "Progress.__init__"
    assert "self.total = total" in initializer.source_code
    assert "unused_35" not in initializer.source_code


def test_locator_can_target_evidenced_initializer_upstream_of_accessor() -> None:
    repo = (
        Path(__file__).resolve().parents[1] / "benchmarks" / "real"
        / "tasks" / "bugsinpy-tqdm-5" / "repo"
    )
    index = build_source_index(repo)
    graph = build_call_graph(index)
    source_file = repo / "tqdm" / "_tqdm.py"
    test_file = repo / "tqdm" / "tests" / "tests_tqdm.py"
    failure = Failure(
        test_id="tqdm/tests/tests_tqdm.py::test_bool", phase="call",
        exception_type="AttributeError",
        message="'tqdm' object has no attribute 'total'",
        traceback_frames=(
            Frame(file_path=test_file, line_number=1591,
                  function_name="test_bool"),
            Frame(file_path=source_file, line_number=869,
                  function_name="__len__"),
        ),
    )
    context = build_relevant_context(
        index, graph, failure, allowed_edit_paths=("tqdm/_tqdm.py",)
    )
    assert context is not None
    evidence = build_diagnostic_evidence(
        index,
        RunResult(
            status=RunStatus.FAILED, command=("pytest",),
            working_path=repo.resolve(), exit_code=1,
            stdout="", stderr="", duration_seconds=0.1,
            testfailures=(failure,),
        ),
        related_context=context,
    )
    targets = {
        (item.file_path, item.symbol_name): item
        for item in evidence if item.kind == "source"
    }
    initializer = targets[(Path("tqdm/_tqdm.py"), "tqdm.__init__")]
    assert "if disable:" in initializer.content
    assert "self.total = total" in initializer.content


def test_assertion_fallback_does_not_guess_between_two_production_calls(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "maths.py").write_text(
        "def first():\n    return 1\n\ndef second():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_maths.py").write_text(
        "from maths import first, second\n"
        "def test_sum():\n    assert first() + second() == 4\n",
        encoding="utf-8",
    )
    index = build_source_index(tmp_path)
    graph = build_call_graph(index)
    failure = Failure(
        test_id="tests/test_maths.py::test_sum",
        phase="call",
        exception_type="AssertionError",
        message="assert 3 == 4",
        traceback_frames=(Frame(
            file_path=tmp_path / "tests" / "test_maths.py",
            line_number=3,
            function_name="test_sum",
        ),),
    )

    assert build_relevant_context(index, graph, failure) is None


def test_assertion_fallback_resolves_unique_imported_method_alias(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "worker.py").write_text(
        "class Worker:\n"
        "    @staticmethod\n"
        "    def calculate(value):\n"
        "        return value + 1\n", encoding="utf-8"
    )
    test_file = tmp_path / "tests" / "test_worker.py"
    test_file.write_text(
        "from worker import Worker\n"
        "def test_calculate():\n"
        "    call = Worker.calculate\n"
        "    assert call(2) == 4\n", encoding="utf-8"
    )
    index = build_source_index(tmp_path)
    graph = build_call_graph(index)
    failure = Failure(
        test_id="tests/test_worker.py::test_calculate",
        phase="call", exception_type="AssertionError", message="assert 3 == 4",
        traceback_frames=(Frame(
            file_path=test_file, line_number=4,
            function_name="test_calculate",
        ),),
    )
    context = build_relevant_context(index, graph, failure)
    assert context is not None
    assert context.target.qualified_name == "Worker.calculate"
    assert context.selection_rule == "unique imported method alias in failing public test"


def test_fixture_method_fallback_requires_one_allowed_matching_function(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    for name in ("fish.py", "other.py"):
        (tmp_path / "src" / name).write_text(
            "class Shell:\n    def info(self):\n        return 'wrong'\n",
            encoding="utf-8",
        )
    test_file = tmp_path / "tests" / "test_fish.py"
    test_file.write_text(
        "def test_info(shell):\n    assert shell.info() == 'right'\n",
        encoding="utf-8",
    )
    index = build_source_index(tmp_path)
    graph = build_call_graph(index)
    failure = Failure(
        test_id="tests/test_fish.py::test_info", phase="call",
        exception_type="AssertionError", message="wrong result",
        traceback_frames=(Frame(
            file_path=test_file, line_number=2, function_name="test_info",
        ),),
    )

    assert build_relevant_context(index, graph, failure) is None
    assert build_relevant_context(
        index, graph, failure, allowed_edit_paths=("src",)
    ) is None
    context = build_relevant_context(
        index, graph, failure, allowed_edit_paths=("src/fish.py",)
    )
    assert context is not None
    assert context.target.relative_path == Path("src/fish.py")
    assert context.target.qualified_name == "Shell.info"
    assert context.selection_rule == "unique allowed function matching failing public test name"


def test_fixture_method_fallback_locates_fish_regression_tasks() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for task_number, test_name, line_number, target_name in (
        (3, "test_info", 118, "Fish.info"),
        (14, "test_get_overridden_aliases", 31, "Fish._get_overridden_aliases"),
    ):
        repo = (
            project_root / "benchmarks" / "real" / "tasks"
            / f"bugsinpy-thefuck-{task_number}" / "repo"
        )
        index = build_source_index(repo)
        graph = build_call_graph(index)
        failure = Failure(
            test_id=f"tests/shells/test_fish.py::TestFish::{test_name}",
            phase="call", exception_type="AssertionError", message="wrong result",
            traceback_frames=(Frame(
                file_path=repo / "tests" / "shells" / "test_fish.py",
                line_number=line_number, function_name=test_name,
            ),),
        )
        context = build_relevant_context(
            index, graph, failure,
            allowed_edit_paths=("thefuck/shells/fish.py",),
        )
        assert context is not None
        assert context.target.qualified_name == target_name
        evidence = build_diagnostic_evidence(
            index,
            RunResult(
                status=RunStatus.FAILED, command=("pytest",),
                working_path=repo.resolve(), exit_code=1, stdout="", stderr="",
                duration_seconds=0.1, testfailures=(failure,),
            ),
            related_context=context,
        )
        assert any(
            item.kind == "source" and item.symbol_name == target_name
            for item in evidence
        )


def test_large_function_source_evidence_focuses_on_public_assertion() -> None:
    repo = (
        Path(__file__).resolve().parents[1] / "benchmarks" / "real"
        / "tasks" / "bugsinpy-tqdm-8" / "repo"
    )
    index = build_source_index(repo)
    graph = build_call_graph(index)
    failure = Failure(
        test_id="tqdm/tests/tests_tqdm.py::test_format_meter",
        phase="call", exception_type="AssertionError", message="unexpected bar text",
        traceback_frames=(Frame(
            file_path=repo / "tqdm" / "tests" / "tests_tqdm.py",
            line_number=218, function_name="test_format_meter",
            source_line=(
                "assert format_meter(20, 100, 12, ncols=13, rate=8.1, "
                "bar_format=r'{l_bar}{bar}|{n_fmt}/{total_fmt}')"
            ),
        ),),
    )
    context = build_relevant_context(index, graph, failure)
    assert context is not None
    assert context.target.qualified_name == "tqdm.format_meter"
    evidence = build_diagnostic_evidence(
        index,
        RunResult(
            status=RunStatus.FAILED, command=("pytest",),
            working_path=repo.resolve(), exit_code=1,
            stdout="", stderr="", duration_seconds=0.1,
            testfailures=(failure,),
        ),
        related_context=context,
    )
    source = next(item.content for item in evidence if item.kind == "source")
    assert "if bar_format:" in source
    assert "bar_format.split('{bar}')" in source
    assert "N_BARS = max" in source
    assert "Parameters" not in source
