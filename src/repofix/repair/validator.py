"""Evaluate a candidate using structural checks and public pytest results."""

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
import ast
import hashlib
import tokenize

from repofix.agents.repair import PatchCandidate
from repofix.analysis import SourceIndex
from repofix.benchmarks.models import TaskSpec
from repofix.execution.models import TestRunResult, TestRunStatus
from repofix.execution.runner import run_test_command
from repofix.repair.patcher import PatchRejected, apply_function_patch, validate_edit_target
from repofix.repair.reviewer import PatchReview, review_python_patch


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: str
    diff: str
    public_test_result: TestRunResult | None
    reason: str
    review: PatchReview | None = None
    patched_ast_fingerprint: str | None = None

    @property
    def public_tests_passed(self) -> bool:
        return (
            self.public_test_result is not None
            and self.public_test_result.status is TestRunStatus.PASSED
        )


def validate_candidate(
    *,
    task: TaskSpec,
    repo_path: Path,
    source_index: SourceIndex,
    candidate: PatchCandidate,
    test_runner: Callable[[Path, str, float], TestRunResult] = run_test_command,
) -> ValidationResult:
    """Apply one candidate only to the workspace, then run public tests."""

    try:
        target = validate_edit_target(task, repo_path, candidate.target_file)
        with tokenize.open(target) as source_file:
            before = source_file.read()
        diff = apply_function_patch(
            task=task,
            repo_path=repo_path,
            source_index=source_index,
            candidate=candidate,
        )
        with tokenize.open(target) as source_file:
            after = source_file.read()
    except (PatchRejected, OSError, UnicodeError, SyntaxError) as error:
        return ValidationResult(
            status="rejected",
            diff="",
            public_test_result=None,
            reason=str(error),
        )

    try:
        before_ast = ast.dump(ast.parse(before), include_attributes=False)
        after_ast = ast.dump(ast.parse(after), include_attributes=False)
    except SyntaxError as error:
        return ValidationResult(
            status="rejected", diff=diff, public_test_result=None,
            reason=f"patched module is not valid Python: {error}",
        )
    if before_ast == after_ast:
        return ValidationResult(
            status="rejected", diff=diff, public_test_result=None,
            reason="patch changes only formatting or comments, not Python code structure",
        )

    review = review_python_patch(before, after)
    if review.status == "rejected":
        return ValidationResult(
            status="rejected", diff=diff, public_test_result=None,
            reason="补丁质量审查拒绝：" + "；".join(
                finding.message for finding in review.findings if finding.severity == "block"
            ),
            review=review,
        )

    result = test_runner(
        working_path=repo_path,
        command=task.public_test_command,
        timeout_seconds=task.timeout_seconds,
    )
    return ValidationResult(
        status=(
            "public_tests_passed"
            if result.status is TestRunStatus.PASSED
            else "public_tests_failed"
        ),
        diff=diff,
        public_test_result=result,
        reason=(
            "Public tests passed; hidden tests have not run."
            if result.status is TestRunStatus.PASSED
            else f"Public test status: {result.status.value}"
        ),
        review=review,
        patched_ast_fingerprint=hashlib.sha256(
            after_ast.encode("utf-8")
        ).hexdigest(),
    )
