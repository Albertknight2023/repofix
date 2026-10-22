"""Exercise one repair attempt without calling a live model service."""

import json
from pathlib import Path

import pytest

from repofix.agents.models import BugHypothesis, EvidenceItem, LocatorResult
from repofix.agents.repair import PatchCandidate, RepairAgent
from repofix.agents.workflow import run_repair_task
from repofix.analysis import build_source_index
from repofix.benchmarks.loader import load_task
from repofix.llm import ScriptedModelClient
from repofix.repair import validate_candidate
from repofix.repair.patcher import PatchRejected, apply_function_patch
from repofix.workspace.manager import isolated_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = (
    PROJECT_ROOT
    / "benchmarks"
    / "tasks"
    / "pagination-zero-page-size"
    / "task.yaml"
)
LOCATOR_RESPONSE = PROJECT_ROOT / "examples" / "pagination-locator-response.json"
REPAIR_RESPONSE = PROJECT_ROOT / "examples" / "pagination-repair-response.json"


def _candidate(
    *,
    target_file: str = "src/pagination.py",
    target_symbol: str = "paginate",
    replacement_source: str,
) -> PatchCandidate:
    return PatchCandidate(
        target_file=Path(target_file),
        target_symbol=target_symbol,
        replacement_source=replacement_source,
        explanation="test candidate",
        evidence_ids=("F1", "S1"),
    )


def test_mock_repair_passes_public_tests_without_changing_benchmark() -> None:
    task = load_task(TASK_PATH)
    original_path = task.repo_path / "src" / "pagination.py"
    original = original_path.read_bytes()
    locator_client = ScriptedModelClient(LOCATOR_RESPONSE.read_text(encoding="utf-8"))
    repair_client = ScriptedModelClient(REPAIR_RESPONSE.read_text(encoding="utf-8"))

    run = run_repair_task(task, locator_client, repair_client)

    assert run.initial_public_test_status.value == "failed"
    assert run.repair_result.status == "patch"
    assert run.validation_result is not None
    assert run.validation_result.status == "public_tests_passed"
    assert run.validation_result.public_tests_passed
    assert "+    if page_size <= 0:" in run.validation_result.diff
    assert original_path.read_bytes() == original
    assert len(locator_client.calls) == len(repair_client.calls) == 1

    repair_request = json.loads(repair_client.calls[0][1].content)
    assert repair_request["hypothesis"]["target_symbol"] == "paginate"
    assert "grader_tests" not in repair_client.calls[0][1].content
    assert "solution.patch" not in repair_client.calls[0][1].content


@pytest.mark.parametrize(
    "path, expected_reason",
    [
        ("tests/test_pagination.py", "cannot modify tests"),
        ("../pagination.py", "repository-relative"),
    ],
)
def test_validator_rejects_forbidden_patch_target(
    path: str, expected_reason: str
) -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=index,
            candidate=_candidate(
                target_file=path,
                replacement_source="def paginate(items, page, page_size):\n    return []\n",
            ),
        )

    assert result.status == "rejected"
    assert expected_reason in result.reason
    assert result.public_test_result is None


@pytest.mark.parametrize(
    "replacement, expected_reason",
    [
        ("def paginate(items, page):\n    return []\n", "signature"),
        (
            "def paginate(items, page, page_size):\n    return []\n"
            "open('tests/test_pagination.py', 'w')\n",
            "exactly one function",
        ),
        ("def paginate(items, page, page_size):\n    return (\n", "invalid Python syntax"),
    ],
)
def test_validator_rejects_structurally_invalid_replacement(
    replacement: str, expected_reason: str
) -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        source_path = workspace.repo_path / "src" / "pagination.py"
        original = source_path.read_bytes()
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=index,
            candidate=_candidate(replacement_source=replacement),
        )
        assert source_path.read_bytes() == original

    assert result.status == "rejected"
    assert expected_reason in result.reason


def test_validator_rejects_comment_only_patch_before_public_tests() -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        source = (workspace.repo_path / "src/pagination.py").read_text(encoding="utf-8")
        replacement = source[source.index("def paginate("):].replace(
            "# ZeroDivisionError when page_size is 0.",
            "# Division by zero happens when page_size is 0.",
        )
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=build_source_index(workspace.repo_path),
            candidate=_candidate(replacement_source=replacement),
        )

    assert result.status == "rejected"
    assert "formatting or comments" in result.reason
    assert result.public_test_result is None


def test_validator_reports_valid_but_ineffective_patch_as_failed() -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=index,
            candidate=_candidate(
                replacement_source=(
                    "def paginate(items: Sequence[T], page: int, page_size: int) -> list[T]:\n"
                    "    total_pages = len(items) // page_size\n"
                    "    return list(items[:page_size])\n"
                ),
            ),
        )

    assert result.status == "public_tests_failed"
    assert not result.public_tests_passed
    assert result.public_test_result is not None
    assert result.public_test_result.testfailures


def test_repair_agent_rejects_unverified_evidence_ids() -> None:
    task = load_task(TASK_PATH)
    response = json.loads(REPAIR_RESPONSE.read_text(encoding="utf-8"))
    response["evidence_ids"] = ["UNKNOWN"]
    repair_client = ScriptedModelClient(json.dumps(response))
    locator_client = ScriptedModelClient(LOCATOR_RESPONSE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="invalid evidence IDs"):
        run_repair_task(task, locator_client, repair_client)


def test_repair_agent_does_not_call_model_without_locator_hypothesis() -> None:
    task = load_task(TASK_PATH)
    locator_client = ScriptedModelClient(
        '{"status":"insufficient_evidence","explanation":"Need more evidence."}'
    )
    repair_client = ScriptedModelClient("unused")

    run = run_repair_task(task, locator_client, repair_client)

    assert run.repair_result.status == "no_patch"
    assert run.validation_result is None
    assert repair_client.calls == []


def test_validator_accepts_small_snippet_instead_of_whole_function() -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=index,
            candidate=PatchCandidate(
                target_file=Path("src/pagination.py"),
                target_symbol="paginate",
                replacement_source="",
                explanation="validate before dividing",
                evidence_ids=("F1", "S1"),
                old_snippet="    total_pages = (len(items) + page_size - 1) // page_size\n",
                new_snippet=(
                    "    if page_size <= 0:\n"
                    "        raise ValueError('page_size must be positive')\n"
                    "    total_pages = (len(items) + page_size - 1) // page_size\n"
                ),
            ),
        )
    assert result.public_tests_passed
    assert "+    if page_size <= 0:" in result.diff


def test_validator_rejects_snippet_that_changes_signature() -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=index,
            candidate=PatchCandidate(
                target_file=Path("src/pagination.py"),
                target_symbol="paginate",
                replacement_source="",
                explanation="bad edit",
                evidence_ids=("S1",),
                old_snippet="page_size: int",
                new_snippet="page_size: str",
            ),
        )
    assert result.status == "rejected"
    assert "signature" in result.reason


def test_single_line_snippet_without_indentation_is_aligned_only_once() -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=index,
            candidate=PatchCandidate(
                target_file=Path("src/pagination.py"),
                target_symbol="paginate",
                replacement_source="",
                explanation="validate before dividing",
                evidence_ids=("F1", "S1"),
                old_snippet="total_pages = (len(items) + page_size - 1) // page_size",
                new_snippet="total_pages = (len(items) + page_size - 1) // max(page_size, 1)",
            ),
        )
    assert result.status != "rejected"
    assert "+    total_pages = (len(items) + page_size - 1) // max(page_size, 1)" in result.diff


def test_single_line_snippet_does_not_convert_noop_to_patch() -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task,
            repo_path=workspace.repo_path,
            source_index=index,
            candidate=PatchCandidate(
                target_file=Path("src/pagination.py"),
                target_symbol="paginate",
                replacement_source="",
                explanation="no change",
                evidence_ids=("F1", "S1"),
                old_snippet="total_pages = (len(items) + page_size - 1) // page_size",
                new_snippet="total_pages = (len(items) + page_size - 1) // page_size",
            ),
        )
    assert result.status == "rejected"
    assert "no change" in result.reason


@pytest.mark.parametrize(
    "old_snippet, expected_reason",
    [
        ("not_present_anywhere", "matched 0 times"),
        ("return", "matched 2 times"),
    ],
)
def test_ambiguous_snippet_feedback_is_actionable(
    old_snippet: str, expected_reason: str,
) -> None:
    task = load_task(TASK_PATH)
    with isolated_workspace(task) as workspace:
        source_file = workspace.repo_path / "src" / "pagination.py"
        original = source_file.read_bytes()
        index = build_source_index(workspace.repo_path)
        result = validate_candidate(
            task=task, repo_path=workspace.repo_path, source_index=index,
            candidate=PatchCandidate(
                target_file=Path("src/pagination.py"),
                target_symbol="paginate", replacement_source="",
                explanation="test rejection", evidence_ids=("S1",),
                old_snippet=old_snippet, new_snippet="changed",
            ),
        )
        assert source_file.read_bytes() == original
    assert result.status == "rejected"
    assert expected_reason in result.reason
    assert "full replacement function" in result.reason


def test_duplicate_nested_function_name_uses_cited_line_and_exact_snippet() -> None:
    task = load_task(
        PROJECT_ROOT / "benchmarks/real/tasks/bugsinpy-PySnooper-3/task.yaml"
    )
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        locator = LocatorResult(
            status="hypothesis",
            hypothesis=BugHypothesis(
                summary="Undefined output_path",
                root_cause="The file writer uses an undefined name.",
                target_file=Path("pysnooper/pysnooper.py"),
                target_symbol="get_write_function.write",
                confidence=0.9,
                evidence_ids=("S2",),
                next_step="Change the file writer.",
            ),
            evidence=(EvidenceItem(
                evidence_id="S2", kind="source", content="file writer",
                file_path=Path("pysnooper/pysnooper.py"),
                line_number=26, symbol_name="get_write_function.write",
            ),),
            explanation="Unverified hypothesis.", model="scripted-mock",
        )
        client = ScriptedModelClient(
            '{"status":"no_patch","explanation":"test"}'
        )
        RepairAgent(client).propose(
            locator_result=locator, source_index=index, related_context=None
        )
        target_source = json.loads(client.calls[0][1].content)["target_source"]
        assert "with open(output_path" in target_source
        assert "stderr.write(s)" not in target_source

        candidate = PatchCandidate(
            target_file=Path("pysnooper/pysnooper.py"),
            target_symbol="get_write_function.write",
            replacement_source="",
            explanation="Use the outer output path.",
            evidence_ids=("S2",),
            old_snippet="with open(output_path, 'a') as output_file:",
            new_snippet="with open(output, 'a') as output_file:",
        )
        diff = apply_function_patch(
            task=task, repo_path=workspace.repo_path,
            source_index=index, candidate=candidate,
        )
        assert "+            with open(output, 'a') as output_file:" in diff


def test_class_method_with_column_zero_docstring_accepts_valid_snippet() -> None:
    task = load_task(
        PROJECT_ROOT / "benchmarks/real/tasks/bugsinpy-tqdm-5/task.yaml"
    )
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        candidate = PatchCandidate(
            target_file=Path("tqdm/_tqdm.py"),
            target_symbol="tqdm.__init__",
            replacement_source="",
            explanation="Initialize total in the disabled branch.",
            evidence_ids=("S2",),
            old_snippet="self.n = initial\n            return",
            new_snippet="self.n = initial\n            self.total = total\n            return",
        )
        diff = apply_function_patch(
            task=task, repo_path=workspace.repo_path,
            source_index=index, candidate=candidate,
        )
        assert "+            self.total = total" in diff


def test_duplicate_nested_function_rejects_ambiguous_whole_function() -> None:
    task = load_task(
        PROJECT_ROOT / "benchmarks/real/tasks/bugsinpy-PySnooper-3/task.yaml"
    )
    with isolated_workspace(task) as workspace:
        index = build_source_index(workspace.repo_path)
        with pytest.raises(PatchRejected, match="ambiguous"):
            apply_function_patch(
                task=task, repo_path=workspace.repo_path,
                source_index=index,
                candidate=PatchCandidate(
                    target_file=Path("pysnooper/pysnooper.py"),
                    target_symbol="get_write_function.write",
                    replacement_source="def write(s):\n    pass\n",
                    explanation="ambiguous", evidence_ids=("S2",),
                ),
            )
