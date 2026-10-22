"""Offline grading: saved patches are checked without involving an Agent."""

import re
import shlex
import shutil
import subprocess
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from repofix.benchmarks.models import TaskSpec
from repofix.execution.models import TestRunStatus
from repofix.execution.models import TestRunResult
from repofix.execution.runner import run_test_command
from repofix.repair.patcher import PatchRejected, validate_edit_target
from repofix.repair.reviewer import PatchReview, review_python_patch
from repofix.workspace.manager import isolated_workspace


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?:.*)$")
_MAX_PATCH_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class GradingResult:
    task_id: str
    status: str
    public_test_status: TestRunStatus | None
    hidden_test_status: TestRunStatus | None
    reason: str
    review: PatchReview | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _patch_target(patch_text: str, task: TaskSpec, repo_path: Path) -> Path:
    """Accept only a one-file unified diff targeting editable production code."""

    lines = patch_text.splitlines()
    git_header = None
    if lines and lines[0].startswith("diff --git "):
        git_header = lines.pop(0)
    if len(lines) < 4 or not lines[0].startswith("--- a/") or not lines[1].startswith("+++ b/"):
        raise PatchRejected("patch must be a one-file unified diff")

    old_name = lines[0][6:]
    new_name = lines[1][6:]
    if old_name != new_name or not old_name or "\\" in old_name or ":" in old_name:
        raise PatchRejected("patch must modify one existing repository-relative file")
    if git_header is not None and git_header != f"diff --git a/{old_name} b/{old_name}":
        raise PatchRejected("patch Git header does not match its target")

    in_hunk = False
    for line in lines[2:]:
        if line.startswith(("diff --git ", "--- a/", "+++ b/")):
            raise PatchRejected("patch must not contain a second file header")
        if line.startswith("@@"):
            if not _HUNK_HEADER.fullmatch(line):
                raise PatchRejected("patch contains an invalid hunk header")
            in_hunk = True
        elif not in_hunk or not line or line[0] not in {" ", "+", "-"}:
            raise PatchRejected("patch contains unsupported content")

    if not in_hunk:
        raise PatchRejected("patch has no hunks")
    return validate_edit_target(task, repo_path, Path(old_name))


def _git_apply(patch_text: str, repo_path: Path) -> bool:
    """Use Git's context checks; the benchmark itself need not be a Git repo."""

    patch_bytes = patch_text.encode("utf-8")
    try:
        check = subprocess.run(
            ["git", "apply", "--check", "-"],
            input=patch_bytes,
            cwd=repo_path,
            capture_output=True,
            timeout=15,
            check=False,
            shell=False,
        )
        if check.returncode != 0:
            return False
        applied = subprocess.run(
            ["git", "apply", "-"],
            input=patch_bytes,
            cwd=repo_path,
            capture_output=True,
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("the local Git patch tool is unavailable") from error
    return applied.returncode == 0


def grade_saved_patch(
    task: TaskSpec,
    patch_path: Path,
    *,
    test_runner: Callable[[Path, str, float], TestRunResult] = run_test_command,
) -> GradingResult:
    """Grade a saved diff in a fresh copy; never return hidden test details."""

    if "../grader_tests" not in shlex.split(task.full_test_command):
        raise ValueError("full_test_command must include ../grader_tests")
    patch_path = patch_path.resolve()
    if not patch_path.is_file():
        raise FileNotFoundError(f"patch file does not exist: {patch_path}")
    if patch_path.stat().st_size > _MAX_PATCH_BYTES:
        return GradingResult(task.id, "invalid_patch", None, None, "Patch exceeds size limit.")
    try:
        patch_text = patch_path.read_text(encoding="utf-8")
    except UnicodeError:
        return GradingResult(task.id, "invalid_patch", None, None, "Patch must be UTF-8 text.")

    with isolated_workspace(task) as workspace:
        try:
            target = _patch_target(patch_text, task, workspace.repo_path)
            with tokenize.open(target) as source_file:
                before = source_file.read()
        except (PatchRejected, OSError, UnicodeError, SyntaxError) as error:
            return GradingResult(task.id, "invalid_patch", None, None, str(error))
        if not _git_apply(patch_text, workspace.repo_path):
            return GradingResult(
                task.id, "invalid_patch", None, None,
                "Patch does not apply cleanly to the benchmark baseline.",
            )

        try:
            with tokenize.open(target) as source_file:
                after = source_file.read()
        except (OSError, UnicodeError, SyntaxError):
            return GradingResult(task.id, "invalid_patch", None, None, "Patched source cannot be decoded.")
        review = review_python_patch(before, after)
        if review.status == "rejected":
            return GradingResult(
                task.id, "rejected_patch", None, None,
                "补丁质量审查拒绝：" + "；".join(
                    finding.message for finding in review.findings if finding.severity == "block"
                ),
                review,
            )

        public = test_runner(
            workspace.repo_path,
            task.public_test_command,
            task.timeout_seconds,
        )
        if public.status is not TestRunStatus.PASSED:
            return GradingResult(
                task.id,
                "failed" if public.status is TestRunStatus.FAILED else "error",
                public.status, None,
                "Public tests did not pass; hidden tests were not run."
                if public.status is TestRunStatus.FAILED
                else "Public test execution failed or timed out; hidden tests were not run.",
                review,
            )

        # Only the offline grader, after the Agent has finished, sees these tests.
        try:
            shutil.copytree(task.grader_test_path, workspace.root / "grader_tests")
        except OSError:
            return GradingResult(
                task.id, "error", public.status, None,
                "Hidden test setup failed.",
                review,
            )
        hidden = test_runner(
            workspace.repo_path,
            task.full_test_command,
            task.timeout_seconds,
        )
        return GradingResult(
            task_id=task.id,
            status=(
                "passed" if hidden.status is TestRunStatus.PASSED
                else "failed" if hidden.status is TestRunStatus.FAILED
                else "error"
            ),
            public_test_status=public.status,
            hidden_test_status=hidden.status,
            reason=(
                "Public and hidden tests passed."
                if hidden.status is TestRunStatus.PASSED
                else "Final acceptance tests did not pass."
            ),
            review=review,
        )
