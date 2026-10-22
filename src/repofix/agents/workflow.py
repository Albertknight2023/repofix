"""Run diagnosis and bounded, evidence-driven repair over benchmark tasks."""

import ast
import hashlib
import textwrap
from dataclasses import dataclass, replace
from collections.abc import Callable
from pathlib import Path

from repofix.agents.locator import LocatorAgent
from repofix.agents.memory import RunMemory
from repofix.agents.direct import DirectRepairAgent
from repofix.agents.models import LocatorResult
from repofix.agents.repair import PatchCandidate, RepairAgent, RepairResult
from repofix.analysis import (
    ContextBundle,
    build_call_graph,
    build_relevant_context,
    build_source_index,
)
from repofix.benchmarks.models import TaskSpec
from repofix.execution.models import TestRunResult, TestRunStatus
from repofix.execution.runner import run_test_command
from repofix.llm import ModelClient
from repofix.repair import ValidationResult, validate_candidate
from repofix.repair.patcher import apply_function_patch
from repofix.workspace.manager import isolated_workspace


COMPETITIVE_ALTERNATIVE_STRATEGIES = (
    "edge-case-complete",
    "alternative-control-flow",
    "contract-first",
)


@dataclass(frozen=True, slots=True)
class LocatorRun:
    task_id: str
    public_test_status: TestRunStatus
    public_test_failures: int
    locator_result: LocatorResult


@dataclass(frozen=True, slots=True)
class RepairRun:
    task_id: str
    initial_public_test_status: TestRunStatus
    initial_test_result: TestRunResult
    related_context: ContextBundle | None
    locator_result: LocatorResult
    repair_result: RepairResult
    validation_result: ValidationResult | None
    workflow_mode: str = "two-agent"
    context_mode: str = "ast-callgraph"
    attempts: tuple["CandidateAttempt", ...] = ()
    selected_attempt: int = 0
    selection_reason: str = "single candidate"
    schedule: tuple["SchedulingDecision", ...] = ()
    memory_snapshot: dict[str, object] | None = None
    locator_attempts: tuple[LocatorResult, ...] = ()

    @property
    def model_calls(self) -> int:
        locators = self.locator_attempts or (self.locator_result,)
        locator_calls = sum(item.model != "none" for item in locators)
        if self.workflow_mode == "single-agent":
            return locator_calls
        return locator_calls + sum(
            attempt.repair_result.model != "none" for attempt in self.attempts
        )

    @property
    def total_prompt_tokens(self) -> int | None:
        locators = self.locator_attempts or (self.locator_result,)
        parts = tuple(item.prompt_tokens for item in locators) + tuple(
            attempt.repair_result.prompt_tokens for attempt in self.attempts
        )
        return sum(value for value in parts if value is not None) if any(
            value is not None for value in parts
        ) else None

    @property
    def total_completion_tokens(self) -> int | None:
        locators = self.locator_attempts or (self.locator_result,)
        parts = tuple(item.completion_tokens for item in locators) + tuple(
            attempt.repair_result.completion_tokens for attempt in self.attempts
        )
        return sum(value for value in parts if value is not None) if any(
            value is not None for value in parts
        ) else None


@dataclass(frozen=True, slots=True)
class CandidateAttempt:
    number: int
    strategy: str
    repair_result: RepairResult
    validation_result: ValidationResult | None
    fingerprint: str | None = None
    locator_result: LocatorResult | None = None
    related_context: ContextBundle | None = None


@dataclass(frozen=True, slots=True)
class SchedulingDecision:
    step: int
    evidence: str
    action: str
    reason: str


def _fingerprint(candidate: PatchCandidate) -> str:
    def normalized(source: str) -> str:
        try:
            return ast.dump(ast.parse(textwrap.dedent(source)), include_attributes=False)
        except SyntaxError:
            return "\n".join(line.rstrip() for line in source.splitlines()).strip()

    material = "\x00".join((
        candidate.target_file.as_posix(), candidate.target_symbol,
        normalized(candidate.replacement_source),
        normalized(candidate.old_snippet or ""),
        normalized(candidate.new_snippet or ""),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _feedback(attempt: CandidateAttempt) -> dict[str, object]:
    """Expose bounded public observations, never private grader outcomes."""
    validation = attempt.validation_result
    result = validation.public_test_result if validation else None
    review = validation.review if validation else None
    return {
        "previous_strategy": attempt.strategy,
        "previous_explanation": (
            attempt.repair_result.candidate.explanation[:600]
            if attempt.repair_result.candidate else attempt.repair_result.explanation[:600]
        ),
        "validation_status": validation.status if validation else "no_patch",
        "validation_reason": validation.reason[:500] if validation else "No patch proposed.",
        "public_failures": [
            {
                "test_id": failure.test_id[:200],
                "exception_type": failure.exception_type[:100],
                "message": failure.message[:400],
            }
            for failure in (result.testfailures[:3] if result else ())
        ],
        "review_findings": [
            {"code": finding.code, "severity": finding.severity}
            for finding in (review.findings if review else ())
        ],
    }


def _rank(attempt: CandidateAttempt) -> tuple[int, int, int, int, int]:
    """Rank only public observations; the private grader never selects a patch."""

    validation = attempt.validation_result
    if validation is None:
        return (0, 0, 0, 0, -attempt.number)
    warnings = sum(
        finding.severity == "warning"
        for finding in (validation.review.findings if validation.review else ())
    )
    changed = validation.review.changed_lines if validation.review else 1000000
    if validation.public_tests_passed:
        return (4, 0, -warnings, -changed, -attempt.number)
    if validation.status == "public_tests_failed":
        result = validation.public_test_result
        if result is not None and result.status is TestRunStatus.FAILED:
            failures = len(result.testfailures) or 1000000
            return (3, -failures, -warnings, -changed, -attempt.number)
        # A timeout or runner error supplies no meaningful failure count.
        return (2, 0, -warnings, -changed, -attempt.number)
    if validation.status == "rejected":
        return (1, 0, 0, 0, -attempt.number)
    return (0, 0, 0, 0, -attempt.number)


def _comparable_patch_identity(attempt: CandidateAttempt) -> str | None:
    """Use the patched AST to ignore cosmetic differences between candidates."""

    validation = attempt.validation_result
    if (
        validation is not None
        and validation.status in {"public_tests_passed", "public_tests_failed"}
        and validation.public_test_result is not None
        and validation.public_test_result.status in {TestRunStatus.PASSED, TestRunStatus.FAILED}
        and validation.diff
    ):
        return validation.patched_ast_fingerprint or validation.diff
    return None


def candidate_comparison(
    attempts: tuple[CandidateAttempt, ...], workflow_mode: str,
) -> dict[str, object]:
    """Expose whether two distinct, structurally accepted patches were compared."""

    eligible_patches = {
        identity for attempt in attempts
        if (identity := _comparable_patch_identity(attempt)) is not None
    }
    status = (
        "not_requested" if workflow_mode in {"single-agent", "two-agent"}
        else "not_triggered" if workflow_mode == "dynamic" and len(attempts) == 1
        else "compared" if len(eligible_patches) >= 2
        else "insufficient_distinct_valid_candidates"
    )
    return {
        "status": status,
        "eligible_count": len(eligible_patches),
        "ranking_rule": (
            "public pass > public failure > timeout/error > rejected > duplicate/no patch; "
            "then fewer failures, fewer review warnings, smaller change, earlier candidate"
        ),
    }


def _retry_reason(initial: TestRunResult, validation: ValidationResult | None) -> str:
    if validation is None:
        return "No first patch was proposed; request an alternative strategy."
    if validation.status == "rejected":
        return "Patch was rejected by structural or policy checks; request a safer alternative."
    if validation.public_tests_passed:
        return "Public tests passed but quality review warned; request a lower-risk alternative."
    if validation.public_test_result is not None:
        before = {failure.test_id for failure in initial.testfailures}
        after = {failure.test_id for failure in validation.public_test_result.testfailures}
        if len(after) > len(before):
            return "More public tests failed after the patch; request an alternative rather than build on it."
        if after == before:
            return "The same public tests still fail; change repair strategy."
        return "Public failure pattern changed but tests still fail; revise from the original workspace."
    return "First candidate did not pass public validation; request an alternative."


def _validate_in_fresh_workspace(
    task: TaskSpec, candidate: PatchCandidate, test_runner: "TestRunner"
) -> ValidationResult:
    with isolated_workspace(task) as workspace:
        return validate_candidate(
            task=task, repo_path=workspace.repo_path,
            source_index=build_source_index(workspace.repo_path),
            candidate=candidate, test_runner=test_runner,
        )


def _rebase_public_result(result: TestRunResult, repo_path: Path) -> TestRunResult:
    """Point candidate traceback files at an equivalent, still-live copy."""

    old_root = result.working_path.resolve()
    new_root = repo_path.resolve()
    failures = []
    for failure in result.testfailures:
        frames = []
        for frame in failure.traceback_frames:
            path = frame.file_path
            if path.is_absolute() and path.is_relative_to(old_root):
                path = new_root / path.relative_to(old_root)
            frames.append(replace(frame, file_path=path))
        failures.append(replace(failure, traceback_frames=tuple(frames)))
    return replace(result, working_path=new_root, testfailures=tuple(failures))


def _relocalize_after_public_failure(
    task: TaskSpec, candidate: PatchCandidate, result: TestRunResult,
    initial_locator: LocatorResult, locator_client: ModelClient,
    context_mode: str,
) -> tuple[LocatorResult, ContextBundle | None]:
    """Re-examine the failed candidate against its actual patched source."""

    with isolated_workspace(task) as workspace:
        baseline_index = build_source_index(workspace.repo_path)
        apply_function_patch(
            task=task, repo_path=workspace.repo_path,
            source_index=baseline_index, candidate=candidate,
        )
        patched_index = build_source_index(workspace.repo_path)
        patched_result = _rebase_public_result(result, workspace.repo_path)
        graph = build_call_graph(patched_index) if context_mode == "ast-callgraph" else None
        context = (
            build_relevant_context(
                patched_index, graph, patched_result.testfailures[0],
                allowed_edit_paths=task.allowed_edit_paths,
            )
            if graph is not None and patched_result.testfailures else None
        )
        prior = initial_locator.hypothesis
        if context is None and prior is not None:
            # A failed patch can move the failure wholly into an assertion in
            # the public test (for example, an expected exception disappears).
            # The edited function remains concrete evidence, but its original
            # diagnosis is not treated as true. Only reuse it if the patched
            # repository still contains that exact production symbol.
            patched_target = next((
                symbol for symbol in patched_index.symbols
                if symbol.relative_path == prior.target_file
                and symbol.qualified_name == prior.target_symbol
                and symbol.is_function
            ), None)
            if patched_target is not None:
                context = ContextBundle(
                    target=patched_target, related=(),
                    selection_rule="previous failed candidate target",
                )
        previous_target = (
            f"{prior.target_file.as_posix()}:{prior.target_symbol}"
            if prior is not None else "none"
        )
        feedback = (
            f"A candidate for {previous_target} failed the public test. "
            "Reassess the root cause and target from the new traceback; "
            "the previous hypothesis is not verified. The next patch will "
            "be applied to a fresh copy of the original repository."
        )
        locator = LocatorAgent(locator_client).diagnose(
            issue=task.issue, test_result=patched_result,
            source_index=patched_index, related_context=context,
            retry_feedback=feedback,
        )
        return locator, context


TestRunner = Callable[[Path, str, float], TestRunResult]


def run_locator_task(
    task: TaskSpec, model_client: ModelClient,
    *, test_runner: TestRunner = run_test_command,
) -> LocatorRun:
    """Run public tests, index the copy, and invoke the locator agent once."""

    with isolated_workspace(task) as workspace:
        test_result = test_runner(
            working_path=workspace.repo_path,
            command=task.public_test_command,
            timeout_seconds=task.timeout_seconds,
        )
        if test_result.status is not TestRunStatus.FAILED:
            raise RuntimeError(
                f"public tests must fail for diagnosis; got {test_result.status}"
            )

        source_index = build_source_index(workspace.repo_path)
        call_graph = build_call_graph(source_index)
        related_context = (
            build_relevant_context(
                source_index,
                call_graph,
                test_result.testfailures[0],
                allowed_edit_paths=task.allowed_edit_paths,
            )
            if test_result.testfailures
            else None
        )
        locator_result = LocatorAgent(model_client).diagnose(
            issue=task.issue,
            test_result=test_result,
            source_index=source_index,
            related_context=related_context,
        )
        return LocatorRun(
            task_id=task.id,
            public_test_status=test_result.status,
            public_test_failures=len(test_result.testfailures),
            locator_result=locator_result,
        )


def run_repair_task(
    task: TaskSpec,
    locator_client: ModelClient,
    repair_client: ModelClient,
    *,
    test_runner: TestRunner = run_test_command,
    workflow_mode: str = "two-agent",
    context_mode: str = "ast-callgraph",
    retry_feedback: str | None = None,
) -> RepairRun:
    """Diagnose and validate isolated candidates with bounded scheduling."""

    if workflow_mode not in {"two-agent", "single-agent", "competitive", "dynamic"}:
        raise ValueError("workflow_mode must be two-agent, single-agent, competitive or dynamic")
    if context_mode not in {"ast-callgraph", "traceback-only"}:
        raise ValueError("context_mode must be ast-callgraph or traceback-only")

    with isolated_workspace(task) as workspace:
        initial_result = test_runner(
            working_path=workspace.repo_path,
            command=task.public_test_command,
            timeout_seconds=task.timeout_seconds,
        )
        if initial_result.status is not TestRunStatus.FAILED:
            raise RuntimeError(
                "public tests must fail before repair; "
                f"got {initial_result.status}"
            )

        source_index = build_source_index(workspace.repo_path)
        call_graph = (
            build_call_graph(source_index)
            if context_mode == "ast-callgraph" else None
        )
        related_context = (
            build_relevant_context(
                source_index, call_graph, initial_result.testfailures[0],
                allowed_edit_paths=task.allowed_edit_paths,
            )
            if initial_result.testfailures and call_graph is not None
            else None
        )
        if workflow_mode == "single-agent":
            locator_result, repair_result = DirectRepairAgent(locator_client).propose(
                issue=task.issue,
                test_result=initial_result,
                source_index=source_index,
                related_context=related_context,
                retry_feedback=retry_feedback,
            )
            proposals = [("direct", repair_result)]
            run_memory = RunMemory.create(
                issue=task.issue, source_index=source_index,
                context=related_context, locator=locator_result,
                initial_result=initial_result,
            )
        else:
            locator_result = LocatorAgent(locator_client).diagnose(
                issue=task.issue,
                test_result=initial_result,
                source_index=source_index,
                related_context=related_context,
                retry_feedback=retry_feedback,
            )
            run_memory = RunMemory.create(
                issue=task.issue, source_index=source_index,
                context=related_context, locator=locator_result,
                initial_result=initial_result,
            )
            strategy = "minimal-change" if workflow_mode in {"competitive", "dynamic"} else "standard"
            repair_result = RepairAgent(repair_client).propose(
                locator_result=locator_result,
                source_index=source_index,
                related_context=related_context,
                strategy=strategy if strategy != "standard" else None,
                memory=run_memory.snapshot() if workflow_mode in {"competitive", "dynamic"} else None,
                feedback=(
                    {"previous_submission_issue": retry_feedback[:500]}
                    if retry_feedback else None
                ),
            )
            proposals = [(strategy, repair_result)]

        locator_attempts = [locator_result]
        independent_memory = run_memory.snapshot()
        attempts: list[CandidateAttempt] = []
        fingerprints: set[str] = set()
        comparable_patches: set[str] = set()
        schedule: list[SchedulingDecision] = [
            SchedulingDecision(1, "initial_public_test:failed", "locate", "Public failure reproduced."),
            SchedulingDecision(
                2, f"locator:{locator_result.status}",
                "generate_candidate" if locator_result.hypothesis else "stop",
                "Locator supplied a hypothesis." if locator_result.hypothesis else "No grounded target was available.",
            ),
        ]

        def register(
            strategy: str, proposal: RepairResult,
            locator: LocatorResult, context: ContextBundle | None,
        ) -> CandidateAttempt:
            candidate = proposal.candidate
            validation: ValidationResult | None = None
            fingerprint: str | None = None
            if candidate is not None:
                fingerprint = _fingerprint(candidate)
                if fingerprint in fingerprints:
                    validation = ValidationResult(
                        status="duplicate", diff="", public_test_result=None,
                        reason="Candidate duplicates an earlier proposal; tests were not repeated.",
                    )
                else:
                    fingerprints.add(fingerprint)
                    validation = _validate_in_fresh_workspace(task, candidate, test_runner)
                    diff = validation.diff
                    publicly_validated = (
                        diff
                        and validation.public_test_result is not None
                        and validation.public_test_result.status in {
                            TestRunStatus.PASSED, TestRunStatus.FAILED,
                        }
                    )
                    patch_identity = validation.patched_ast_fingerprint or diff
                    if publicly_validated and patch_identity in comparable_patches:
                        validation = replace(
                            validation, status="duplicate", public_test_result=None,
                            reason="Candidate produces the same patched code structure "
                            "as an earlier publicly validated proposal; it does not "
                            "count as a second patch.",
                        )
                    elif publicly_validated:
                        comparable_patches.add(patch_identity)
            attempt = CandidateAttempt(
                len(attempts) + 1, strategy, proposal, validation, fingerprint,
                locator, context,
            )
            attempts.append(attempt)
            run_memory.record(
                number=attempt.number, strategy=strategy, fingerprint=fingerprint,
                repair=proposal, validation=validation,
            )
            return attempt

        first = register(*proposals[0], locator_result, related_context)
        if workflow_mode == "competitive" and locator_result.hypothesis is not None:
            for strategy in COMPETITIVE_ALTERNATIVE_STRATEGIES:
                if len(comparable_patches) >= 2:
                    break
                schedule.append(SchedulingDecision(
                    len(schedule) + 1,
                    f"distinct_public_candidates:{len(comparable_patches)}",
                    "generate_second_candidate" if len(attempts) == 1 else "regenerate_distinct_candidate",
                    "Generate a different strategy from the original evidence without "
                    "seeing earlier patch contents or public test feedback.",
                ))
                alternative = RepairAgent(repair_client).propose(
                    locator_result=locator_result,
                    source_index=source_index,
                    related_context=related_context,
                    strategy=strategy,
                    feedback=None,
                    memory=independent_memory,
                )
                register(strategy, alternative, locator_result, related_context)
            if len(comparable_patches) < 2:
                schedule.append(SchedulingDecision(
                    len(schedule) + 1,
                    f"distinct_public_candidates:{len(comparable_patches)}",
                    "competition_incomplete",
                    "The bounded alternative strategies did not yield two distinct, "
                    "publicly validated code changes.",
                ))
        elif workflow_mode == "dynamic" and locator_result.hypothesis is not None:
            first_validation = first.validation_result
            clean_pass = (
                first_validation is not None
                and first_validation.public_tests_passed
                and first_validation.review is not None
                and first_validation.review.status == "passed"
            )
            if not clean_pass:
                second_locator = locator_result
                second_context = related_context
                if (
                    first_validation is not None
                    and first_validation.public_test_result is not None
                    and first_validation.public_test_result.status is TestRunStatus.FAILED
                    and first.repair_result.candidate is not None
                ):
                    second_locator, second_context = _relocalize_after_public_failure(
                        task, first.repair_result.candidate,
                        first_validation.public_test_result, locator_result,
                        locator_client, context_mode,
                    )
                    locator_attempts.append(second_locator)
                    schedule.append(SchedulingDecision(
                        len(schedule) + 1,
                        "candidate_1:public_tests_failed",
                        "relocalize",
                        "Reassess the root cause against the failed patch and its public traceback.",
                    ))
                    revised_hypothesis = second_locator.hypothesis
                    if revised_hypothesis is not None:
                        target_exists = any(
                            symbol.relative_path == revised_hypothesis.target_file
                            and symbol.qualified_name == revised_hypothesis.target_symbol
                            and symbol.is_function
                            for symbol in source_index.symbols
                        )
                        if target_exists:
                            run_memory.relocalize(
                                locator=second_locator,
                                result=first_validation.public_test_result,
                                context=second_context,
                            )
                        else:
                            schedule.append(SchedulingDecision(
                                len(schedule) + 1, "relocalize:target_unavailable",
                                "stop_generation",
                                "The revised target is not a function in the original repository.",
                            ))
                            second_locator = replace(second_locator, hypothesis=None)
                if second_locator.hypothesis is not None:
                    schedule.append(SchedulingDecision(
                        len(schedule) + 1,
                        f"candidate_1:{first_validation.status if first_validation else 'no_patch'}",
                        "generate_second_candidate",
                        _retry_reason(initial_result, first_validation),
                    ))
                    second = RepairAgent(repair_client).propose(
                        locator_result=second_locator,
                        source_index=source_index,
                        related_context=second_context,
                        strategy="edge-case-complete",
                        feedback=_feedback(first),
                        memory=run_memory.snapshot(),
                    )
                    register("edge-case-complete", second, second_locator, second_context)
                else:
                    if not schedule or schedule[-1].action != "stop_generation":
                        schedule.append(SchedulingDecision(
                            len(schedule) + 1, "relocalize:insufficient_evidence",
                            "stop_generation",
                            "No evidence-grounded target remains for a second candidate.",
                        ))
            else:
                schedule.append(SchedulingDecision(
                    len(schedule) + 1, "candidate_1:clean_public_pass", "stop_generation",
                    "Dynamic mode avoids a second model call after a clean pass.",
                ))
        elif workflow_mode in {"single-agent", "two-agent"}:
            schedule.append(SchedulingDecision(
                len(schedule) + 1, "baseline:one_candidate", "stop_generation",
                "Baseline mode is limited to one proposal.",
            ))

        selected = max(attempts, key=_rank)
        comparison = candidate_comparison(tuple(attempts), workflow_mode)
        selected_validation = selected.validation_result
        warnings = sum(
            finding.severity == "warning"
            for finding in (
                selected_validation.review.findings
                if selected_validation and selected_validation.review else ()
            )
        )
        changed_lines = (
            selected_validation.review.changed_lines
            if selected_validation and selected_validation.review else None
        )
        selection_reason = (
            f"Candidate {selected.number} selected from {len(attempts)}: "
            f"status={selected_validation.status if selected_validation else 'no_patch'}, "
            f"review_warnings={warnings}, changed_lines={changed_lines}. "
            f"Comparison: {comparison['status']} ({comparison['eligible_count']} eligible). "
            f"Priority: {comparison['ranking_rule']}."
        )
        schedule.append(SchedulingDecision(
            len(schedule) + 1, f"attempts:{len(attempts)}", f"select_candidate_{selected.number}",
            selection_reason,
        ))
        return RepairRun(
            task_id=task.id,
            initial_public_test_status=initial_result.status,
            initial_test_result=initial_result,
            related_context=selected.related_context,
            locator_result=selected.locator_result or locator_result,
            repair_result=selected.repair_result,
            validation_result=selected.validation_result,
            workflow_mode=workflow_mode,
            context_mode=context_mode,
            attempts=tuple(attempts),
            selected_attempt=selected.number,
            selection_reason=selection_reason,
            schedule=tuple(schedule),
            memory_snapshot=run_memory.snapshot(),
            locator_attempts=tuple(locator_attempts),
        )
