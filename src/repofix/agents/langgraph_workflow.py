"""Optional LangGraph orchestration of RepoFix's existing repair primitives.

The graph changes scheduling, not trust boundaries: model clients only propose
diagnoses and edits; the existing validator runs public tests in fresh copies.
No checkpointer is enabled in this first adapter, so retries remain controlled
by RepoFix's bounded workflow and formal experiment protocol.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from repofix.agents.direct import DirectRepairAgent
from repofix.agents.locator import LocatorAgent
from repofix.agents.memory import RunMemory
from repofix.agents.models import LocatorResult
from repofix.agents.repair import RepairAgent, RepairResult
from repofix.agents.workflow import (
    COMPETITIVE_ALTERNATIVE_STRATEGIES,
    CandidateAttempt,
    RepairRun,
    SchedulingDecision,
    TestRunner,
    _feedback,
    _fingerprint,
    _rank,
    _relocalize_after_public_failure,
    _retry_reason,
    _validate_in_fresh_workspace,
    candidate_comparison,
)
from repofix.analysis import (
    ContextBundle,
    SourceIndex,
    build_call_graph,
    build_relevant_context,
    build_source_index,
)
from repofix.benchmarks.models import TaskSpec
from repofix.execution.models import TestRunResult, TestRunStatus
from repofix.execution.runner import run_test_command
from repofix.llm import ModelClient
from repofix.repair import ValidationResult
from repofix.workspace.manager import isolated_workspace


class RepairGraphState(TypedDict, total=False):
    initial_result: TestRunResult
    source_index: SourceIndex
    related_context: ContextBundle | None
    locator_result: LocatorResult
    current_locator: LocatorResult
    current_context: ContextBundle | None
    locator_attempts: tuple[LocatorResult, ...]
    memory: RunMemory
    independent_memory: dict[str, object]
    proposal: RepairResult
    strategy: str
    proposal_stage: str
    remaining_strategies: tuple[str, ...]
    attempts: tuple[CandidateAttempt, ...]
    fingerprints: frozenset[str]
    comparable_patches: frozenset[str]
    schedule: tuple[SchedulingDecision, ...]
    next_step: str
    run: RepairRun


def run_repair_task_langgraph(
    task: TaskSpec,
    locator_client: ModelClient,
    repair_client: ModelClient,
    *,
    test_runner: TestRunner = run_test_command,
    workflow_mode: str = "two-agent",
    context_mode: str = "ast-callgraph",
    retry_feedback: str | None = None,
) -> RepairRun:
    """Run RepoFix's four workflows as real graph nodes and conditional edges.

    Existing public-only agents, patch checks, budget-tracked model clients and
    hidden-test separation are reused. The legacy engine remains the default.
    """

    if workflow_mode not in {"single-agent", "two-agent", "dynamic", "competitive"}:
        raise ValueError("invalid workflow_mode")
    if context_mode not in {"ast-callgraph", "traceback-only"}:
        raise ValueError("invalid context_mode")

    with isolated_workspace(task) as workspace:
        repo_path: Path = workspace.repo_path

        def prepare(_: RepairGraphState) -> RepairGraphState:
            result = test_runner(
                working_path=repo_path,
                command=task.public_test_command,
                timeout_seconds=task.timeout_seconds,
            )
            if result.status is not TestRunStatus.FAILED:
                raise RuntimeError(
                    f"public tests must fail before repair; got {result.status}"
                )
            source_index = build_source_index(repo_path)
            call_graph = (
                build_call_graph(source_index)
                if context_mode == "ast-callgraph" else None
            )
            context = (
                build_relevant_context(
                    source_index, call_graph, result.testfailures[0],
                    allowed_edit_paths=task.allowed_edit_paths,
                )
                if result.testfailures and call_graph is not None else None
            )
            return {
                "initial_result": result,
                "source_index": source_index,
                "related_context": context,
            }

        def diagnose(state: RepairGraphState) -> RepairGraphState:
            initial = state["initial_result"]
            index = state["source_index"]
            context = state["related_context"]
            if workflow_mode == "single-agent":
                locator, proposal = DirectRepairAgent(locator_client).propose(
                    issue=task.issue, test_result=initial, source_index=index,
                    related_context=context, retry_feedback=retry_feedback,
                )
                extra: RepairGraphState = {
                    "proposal": proposal, "strategy": "direct",
                }
            else:
                locator = LocatorAgent(locator_client).diagnose(
                    issue=task.issue, test_result=initial, source_index=index,
                    related_context=context, retry_feedback=retry_feedback,
                )
                extra = {
                    "strategy": (
                        "minimal-change" if workflow_mode in {"dynamic", "competitive"}
                        else "standard"
                    ),
                }
            memory = RunMemory.create(
                issue=task.issue, source_index=index, context=context,
                locator=locator, initial_result=initial,
            )
            schedule = (
                SchedulingDecision(
                    1, "initial_public_test:failed", "locate",
                    "Public failure reproduced.",
                ),
                SchedulingDecision(
                    2, f"locator:{locator.status}",
                    "generate_candidate" if locator.hypothesis else "stop",
                    "Locator supplied a hypothesis." if locator.hypothesis
                    else "No grounded target was available.",
                ),
            )
            return {
                **extra,
                "locator_result": locator,
                "current_locator": locator,
                "current_context": context,
                "locator_attempts": (locator,),
                "memory": memory,
                "independent_memory": memory.snapshot(),
                "proposal_stage": "initial",
                "remaining_strategies": COMPETITIVE_ALTERNATIVE_STRATEGIES,
                "attempts": (),
                "fingerprints": frozenset(),
                "comparable_patches": frozenset(),
                "schedule": schedule,
            }

        def generate(state: RepairGraphState) -> RepairGraphState:
            if workflow_mode == "single-agent":
                # DirectRepairAgent already produced the proposal in diagnose.
                return {}
            stage = state["proposal_stage"]
            strategy = state["strategy"]
            proposal = RepairAgent(repair_client).propose(
                locator_result=state["current_locator"],
                source_index=state["source_index"],
                related_context=state["current_context"],
                strategy=strategy if strategy != "standard" else None,
                memory=(
                    state["independent_memory"] if stage == "alternative"
                    else state["memory"].snapshot()
                    if workflow_mode in {"dynamic", "competitive"} else None
                ),
                feedback=(
                    _feedback(state["attempts"][0]) if stage == "dynamic_second"
                    else {"previous_submission_issue": retry_feedback[:500]}
                    if stage == "initial" and retry_feedback else None
                ),
            )
            return {"proposal": proposal}

        def validate(state: RepairGraphState) -> RepairGraphState:
            proposal = state["proposal"]
            candidate = proposal.candidate
            fingerprints = set(state["fingerprints"])
            comparable = set(state["comparable_patches"])
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
                        diff and validation.public_test_result is not None
                        and validation.public_test_result.status in {
                            TestRunStatus.PASSED, TestRunStatus.FAILED,
                        }
                    )
                    identity = validation.patched_ast_fingerprint or diff
                    if publicly_validated and identity in comparable:
                        validation = replace(
                            validation, status="duplicate", public_test_result=None,
                            reason="Candidate produces the same patched code structure "
                            "as an earlier publicly validated proposal; it does not "
                            "count as a second patch.",
                        )
                    elif publicly_validated:
                        comparable.add(identity)
            attempts = state["attempts"]
            attempt = CandidateAttempt(
                len(attempts) + 1, state["strategy"], proposal, validation,
                fingerprint, state["current_locator"], state["current_context"],
            )
            state["memory"].record(
                number=attempt.number, strategy=attempt.strategy,
                fingerprint=fingerprint, repair=proposal, validation=validation,
            )
            return {
                "attempts": (*attempts, attempt),
                "fingerprints": frozenset(fingerprints),
                "comparable_patches": frozenset(comparable),
            }

        def decide(state: RepairGraphState) -> RepairGraphState:
            attempts = state["attempts"]
            schedule = state["schedule"]
            first = attempts[0]
            if workflow_mode == "competitive" and state["locator_result"].hypothesis:
                if len(state["comparable_patches"]) >= 2:
                    return {"next_step": "select"}
                remaining = state["remaining_strategies"]
                if remaining:
                    action = (
                        "generate_second_candidate" if len(attempts) == 1
                        else "regenerate_distinct_candidate"
                    )
                    decision = SchedulingDecision(
                        len(schedule) + 1,
                        f"distinct_public_candidates:{len(state['comparable_patches'])}",
                        action,
                        "Generate a different strategy from the original evidence "
                        "without seeing earlier patch contents or public test feedback.",
                    )
                    return {
                        "next_step": "generate", "proposal_stage": "alternative",
                        "strategy": remaining[0],
                        "remaining_strategies": remaining[1:],
                        "current_locator": state["locator_result"],
                        "current_context": state["related_context"],
                        "schedule": (*schedule, decision),
                    }
                return {
                    "next_step": "select",
                    "schedule": (*schedule, SchedulingDecision(
                        len(schedule) + 1,
                        f"distinct_public_candidates:{len(state['comparable_patches'])}",
                        "competition_incomplete",
                        "The bounded alternative strategies did not yield two distinct, "
                        "publicly validated code changes.",
                    )),
                }
            if workflow_mode == "dynamic" and state["locator_result"].hypothesis:
                if len(attempts) > 1:
                    return {"next_step": "select"}
                validation = first.validation_result
                clean_pass = (
                    validation is not None and validation.public_tests_passed
                    and validation.review is not None
                    and validation.review.status == "passed"
                )
                if clean_pass:
                    return {
                        "next_step": "select",
                        "schedule": (*schedule, SchedulingDecision(
                            len(schedule) + 1, "candidate_1:clean_public_pass",
                            "stop_generation",
                            "Dynamic mode avoids a second model call after a clean pass.",
                        )),
                    }
                if (
                    validation is not None
                    and validation.public_test_result is not None
                    and validation.public_test_result.status is TestRunStatus.FAILED
                    and first.repair_result.candidate is not None
                ):
                    return {"next_step": "relocalize"}
                return {
                    "next_step": "generate", "proposal_stage": "dynamic_second",
                    "strategy": "edge-case-complete",
                    "schedule": (*schedule, SchedulingDecision(
                        len(schedule) + 1,
                        f"candidate_1:{validation.status if validation else 'no_patch'}",
                        "generate_second_candidate",
                        _retry_reason(state["initial_result"], validation),
                    )),
                }
            return {
                "next_step": "select",
                "schedule": (*schedule, SchedulingDecision(
                    len(schedule) + 1, "baseline:one_candidate",
                    "stop_generation", "Baseline mode is limited to one proposal.",
                )) if workflow_mode in {"single-agent", "two-agent"} else schedule,
            }

        def relocalize(state: RepairGraphState) -> RepairGraphState:
            first = state["attempts"][0]
            validation = first.validation_result
            assert validation is not None and validation.public_test_result is not None
            assert first.repair_result.candidate is not None
            second_locator, second_context = _relocalize_after_public_failure(
                task, first.repair_result.candidate,
                validation.public_test_result, state["locator_result"],
                locator_client, context_mode,
            )
            observed_locator = second_locator
            schedule = (*state["schedule"], SchedulingDecision(
                len(state["schedule"]) + 1,
                "candidate_1:public_tests_failed", "relocalize",
                "Reassess the root cause against the failed patch and its public traceback.",
            ))
            revised = second_locator.hypothesis
            if revised is not None:
                target_exists = any(
                    symbol.relative_path == revised.target_file
                    and symbol.qualified_name == revised.target_symbol
                    and symbol.is_function
                    for symbol in state["source_index"].symbols
                )
                if target_exists:
                    state["memory"].relocalize(
                        locator=second_locator,
                        result=validation.public_test_result,
                        context=second_context,
                    )
                else:
                    schedule = (*schedule, SchedulingDecision(
                        len(schedule) + 1, "relocalize:target_unavailable",
                        "stop_generation",
                        "The revised target is not a function in the original repository.",
                    ))
                    second_locator = replace(second_locator, hypothesis=None)
            if second_locator.hypothesis is None:
                if schedule[-1].action != "stop_generation":
                    schedule = (*schedule, SchedulingDecision(
                        len(schedule) + 1, "relocalize:insufficient_evidence",
                        "stop_generation",
                        "No evidence-grounded target remains for a second candidate.",
                    ))
                next_step = "select"
            else:
                schedule = (*schedule, SchedulingDecision(
                    len(schedule) + 1, f"candidate_1:{validation.status}",
                    "generate_second_candidate",
                    _retry_reason(state["initial_result"], validation),
                ))
                next_step = "generate"
            return {
                "current_locator": second_locator,
                "current_context": second_context,
                "locator_attempts": (*state["locator_attempts"], observed_locator),
                "proposal_stage": "dynamic_second", "strategy": "edge-case-complete",
                "schedule": schedule, "next_step": next_step,
            }

        def select(state: RepairGraphState) -> RepairGraphState:
            attempts = state["attempts"]
            selected = max(attempts, key=_rank)
            comparison = candidate_comparison(attempts, workflow_mode)
            validation = selected.validation_result
            warnings = sum(
                finding.severity == "warning"
                for finding in (validation.review.findings if validation and validation.review else ())
            )
            changed_lines = (
                validation.review.changed_lines if validation and validation.review else None
            )
            reason = (
                f"Candidate {selected.number} selected from {len(attempts)}: "
                f"status={validation.status if validation else 'no_patch'}, "
                f"review_warnings={warnings}, changed_lines={changed_lines}. "
                f"Comparison: {comparison['status']} ({comparison['eligible_count']} eligible). "
                f"Priority: {comparison['ranking_rule']}."
            )
            schedule = (*state["schedule"], SchedulingDecision(
                len(state["schedule"]) + 1,
                f"attempts:{len(attempts)}", f"select_candidate_{selected.number}",
                reason,
            ))
            initial = state["initial_result"]
            return {"run": RepairRun(
                task_id=task.id,
                initial_public_test_status=initial.status,
                initial_test_result=initial,
                related_context=selected.related_context,
                locator_result=selected.locator_result or state["locator_result"],
                repair_result=selected.repair_result,
                validation_result=selected.validation_result,
                workflow_mode=workflow_mode,
                context_mode=context_mode,
                attempts=attempts,
                selected_attempt=selected.number,
                selection_reason=reason,
                schedule=schedule,
                memory_snapshot=state["memory"].snapshot(),
                locator_attempts=state["locator_attempts"],
                execution_engine="langgraph",
            )}

        builder = StateGraph(RepairGraphState)
        builder.add_node("prepare", prepare)
        builder.add_node("diagnose", diagnose)
        builder.add_node("generate", generate)
        builder.add_node("validate", validate)
        builder.add_node("decide", decide)
        builder.add_node("relocalize", relocalize)
        builder.add_node("select", select)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "diagnose")
        builder.add_edge("diagnose", "generate")
        builder.add_edge("generate", "validate")
        builder.add_edge("validate", "decide")
        builder.add_conditional_edges(
            "decide", lambda state: state["next_step"],
            {"generate": "generate", "relocalize": "relocalize", "select": "select"},
        )
        builder.add_conditional_edges(
            "relocalize", lambda state: state["next_step"],
            {"generate": "generate", "select": "select"},
        )
        builder.add_edge("select", END)
        result = builder.compile().invoke({}, {"recursion_limit": 30})
        return result["run"]
