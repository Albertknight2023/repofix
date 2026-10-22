"""Persist auditable repair attempts after their temporary workspace closes."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from repofix.agents.workflow import RepairRun, _rank, candidate_comparison
from repofix.benchmarks.models import TaskSpec
from repofix.execution.models import TestRunResult


@dataclass(frozen=True, slots=True)
class SavedRepairArtifacts:
    run_directory: Path
    report_path: Path
    patch_path: Path | None
    markdown_path: Path | None = None


def _display_path(path: Path, working_path: Path) -> str:
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(working_path.resolve()).as_posix()
    except ValueError:
        return path.name


def _test_summary(result: TestRunResult) -> dict[str, object]:
    return {
        "status": result.status.value,
        "command": list(result.command),
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "failures": [
            {
                "test_id": failure.test_id,
                "phase": failure.phase,
                "exception_type": failure.exception_type,
                "message": failure.message,
                "traceback_frames": [
                    {
                        "file_path": _display_path(
                            frame.file_path, result.working_path
                        ),
                        "line_number": frame.line_number,
                        "function_name": frame.function_name,
                        "source_line": frame.source_line,
                    }
                    for frame in failure.traceback_frames
                ],
            }
            for failure in result.testfailures
        ],
    }


def _evidence_chain(run: RepairRun, patch_filename: str | None) -> list[dict[str, object]]:
    """Distinguish observations, model hypotheses and proposed changes."""
    locator = run.locator_result
    hypothesis = locator.hypothesis
    candidate = run.repair_result.candidate
    validation = run.validation_result
    failure = run.initial_test_result.testfailures[0] if run.initial_test_result.testfailures else None
    claims: list[dict[str, object]] = [{
        "id": "C1", "kind": "observation", "status": "observed",
        "statement": (
            f"Public test {failure.test_id} failed with {failure.exception_type}: {failure.message}"
            if failure else "The initial public test run failed."
        ),
        "evidence_refs": ["F1"] if (
            (not run.locator_attempts or locator is run.locator_attempts[0])
            and any(item.evidence_id == "F1" for item in locator.evidence)
        ) else [],
        "artifact_refs": ["initial_public_test"],
    }]
    if hypothesis is not None:
        claims.append({
            "id": "C2", "kind": "model_hypothesis", "status": "unverified",
            "statement": hypothesis.root_cause,
            "evidence_refs": list(hypothesis.evidence_ids),
            "artifact_refs": ["diagnosis.hypothesis"],
        })
    if candidate is not None:
        claims.append({
            "id": "C3", "kind": "proposed_change",
            "status": "applied_in_temporary_copy" if validation and validation.diff else "not_applied",
            "statement": candidate.explanation,
            "evidence_refs": list(candidate.evidence_ids),
            "artifact_refs": [patch_filename] if patch_filename else [],
        })
    if validation is not None and validation.review is not None:
        claims.append({
            "id": "C4", "kind": "rule_observation", "status": "observed",
            "statement": f"Patch policy review: {validation.review.status}; changed lines: {validation.review.changed_lines}.",
            "evidence_refs": [], "artifact_refs": ["validation.patch_review"],
        })
    if validation is not None and validation.public_test_result is not None:
        claims.append({
            "id": "C5", "kind": "test_observation", "status": "observed",
            "statement": f"Selected candidate public tests: {validation.public_test_result.status.value}.",
            "evidence_refs": [], "artifact_refs": ["validation.public_test"],
        })
    return claims


def _markdown_text(value: object) -> str:
    text = " ".join(str(value).split())[:1000]
    for character in "\\`*_[]<>|":
        text = text.replace(character, "\\" + character)
    return text


def render_repair_markdown(report: dict[str, object]) -> str:
    """Produce a readable audit view from structured facts, not model prose alone."""
    task = report["task"]
    assert isinstance(task, dict)
    selection = report["candidate_selection"]
    assert isinstance(selection, dict)
    lines = [
        f"# RepoFix 修复证据报告：{_markdown_text(task['id'])}", "",
        "本报告区分测试观测、模型假设和规则审查。隐藏测试未在修复阶段运行；公开测试通过不等于最终正确。", "",
        "## 调度与候选选择", "",
    ]
    for event in report["schedule"]:
        lines.append(
            f"- 步骤 {event['step']}：{_markdown_text(event['evidence'])} → "
            f"{_markdown_text(event['action'])}；{_markdown_text(event['reason'])}"
        )
    if len(report.get("diagnostic_rounds", [])) > 1:
        lines.extend(("", "## 重新定位", ""))
        for round_record in report["diagnostic_rounds"]:
            lines.append(
                f"- 第 {round_record['number']} 轮（{_markdown_text(round_record['phase'])}）："
                f"{_markdown_text(round_record['status'])}；目标："
                f"{_markdown_text(round_record['target_file'] or '无')}:"
                f"{_markdown_text(round_record['target_symbol'] or '无')}。"
            )
    comparison = selection["comparison"]
    lines.extend((
        "", f"选中候选：第 {selection['selected_attempt']} 份。",
        f"比较状态：{_markdown_text(comparison['status'])}；"
        f"可比较补丁数：{comparison['eligible_count']}。",
        f"排序规则：{_markdown_text(comparison['ranking_rule'])}。",
        "", "## 候选记录", "",
    ))
    for attempt in selection["attempts"]:
        lines.append(
            f"- 候选 {attempt['number']}（{_markdown_text(attempt['strategy'])}）："
            f"{_markdown_text(attempt['validation_status'] or attempt['repair_status'])}；"
            f"排序键：{_markdown_text(attempt['rank_key'])}；"
            f"补丁文件：{_markdown_text(attempt['patch'] or '无')}。"
        )
    lines.extend(("", "## 可核查结论", ""))
    for claim in report["evidence_chain"]:
        refs = ", ".join(claim["evidence_refs"] + claim["artifact_refs"])
        lines.append(
            f"- {claim['id']} [{_markdown_text(claim['kind'])} / {_markdown_text(claim['status'])}] "
            f"{_markdown_text(claim['statement'])} 来源：{_markdown_text(refs or '无')}。"
        )
    lines.extend(("", "## 未验证与限制", ""))
    lines.extend(f"- {_markdown_text(item)}" for item in report["not_verified"])
    return "\n".join(lines) + "\n"


def build_repair_report(
    *,
    task: TaskSpec,
    run: RepairRun,
    run_id: str,
    created_at_utc: str,
    patch_filename: str | None,
    attempt_patch_filenames: dict[int, str] | None = None,
) -> dict[str, object]:
    """Link model claims to checked input evidence and test observations."""

    locator = run.locator_result
    hypothesis = locator.hypothesis
    repair = run.repair_result
    candidate = repair.candidate
    validation = run.validation_result
    context = run.related_context
    locator_rounds = run.locator_attempts or (locator,)

    attempt_patches = attempt_patch_filenames or {}
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_at_utc": created_at_utc,
        "workflow_mode": run.workflow_mode,
        "context_mode": run.context_mode,
        "task": {
            "id": task.id,
            "title": task.title,
            "issue": task.issue,
        },
        "initial_public_test": _test_summary(run.initial_test_result),
        "evidence": [
            {
                "id": item.evidence_id,
                "kind": item.kind,
                "file_path": item.file_path.as_posix() if item.file_path else None,
                "line_number": item.line_number,
                "symbol_name": item.symbol_name,
                "content": item.content,
            }
            for item in locator.evidence
        ],
        "context_selection": (
            {
                "rule": context.selection_rule,
                "target": {
                    "file_path": context.target.relative_path.as_posix(),
                    "symbol_name": context.target.qualified_name,
                },
                "related": [
                    {
                        "file_path": item.file_path.as_posix(),
                        "symbol_name": item.symbol_name,
                        "relation": item.relation,
                        "call_line": item.call_line,
                        "source_code": item.source_code,
                    }
                    for item in context.related
                ],
            }
            if context is not None
            else None
        ),
        "diagnosis": {
            "status": locator.status,
            "explanation": locator.explanation,
            "hypothesis": (
                {
                    "summary": hypothesis.summary,
                    "root_cause": hypothesis.root_cause,
                    "target_file": hypothesis.target_file.as_posix(),
                    "target_symbol": hypothesis.target_symbol,
                    "confidence": hypothesis.confidence,
                    "evidence_ids": list(hypothesis.evidence_ids),
                    "next_step": hypothesis.next_step,
                }
                if hypothesis is not None
                else None
            ),
        },
        "diagnostic_rounds": [
            {
                "number": number,
                "phase": "initial_public_failure" if number == 1 else "after_candidate_1_failure",
                "status": item.status,
                "explanation": item.explanation,
                "target_file": (
                    item.hypothesis.target_file.as_posix() if item.hypothesis else None
                ),
                "target_symbol": item.hypothesis.target_symbol if item.hypothesis else None,
                "root_cause": item.hypothesis.root_cause if item.hypothesis else None,
                "evidence_ids": list(item.hypothesis.evidence_ids) if item.hypothesis else [],
                "evidence": [
                    {
                        "id": evidence.evidence_id,
                        "kind": evidence.kind,
                        "file_path": evidence.file_path.as_posix() if evidence.file_path else None,
                        "line_number": evidence.line_number,
                        "symbol_name": evidence.symbol_name,
                        "content": evidence.content,
                    }
                    for evidence in item.evidence
                ],
                "model": item.model,
                "prompt_tokens": item.prompt_tokens,
                "completion_tokens": item.completion_tokens,
            }
            for number, item in enumerate(locator_rounds, start=1)
        ],
        "repair": {
            "status": repair.status,
            "explanation": repair.explanation,
            "candidate": (
                {
                    "target_file": candidate.target_file.as_posix(),
                    "target_symbol": candidate.target_symbol,
                    "explanation": candidate.explanation,
                    "evidence_ids": list(candidate.evidence_ids),
                    "replacement_source": candidate.replacement_source,
                    "edit_kind": "snippet" if candidate.old_snippet is not None else "function",
                    "old_snippet": candidate.old_snippet,
                    "new_snippet": candidate.new_snippet,
                }
                if candidate is not None
                else None
            ),
        },
        "validation": (
            {
                "status": validation.status,
                "reason": validation.reason,
                "public_tests_passed": validation.public_tests_passed,
                "public_test": (
                    _test_summary(validation.public_test_result)
                    if validation.public_test_result is not None
                    else None
                ),
                "hidden_tests_run": False,
                "patch_review": validation.review.to_dict() if validation.review else None,
            }
            if validation is not None
            else None
        ),
        "candidate_selection": {
            "selected_attempt": run.selected_attempt,
            "reason": run.selection_reason,
            "comparison": candidate_comparison(run.attempts, run.workflow_mode),
            "attempts": [
                {
                    "number": attempt.number,
                    "strategy": attempt.strategy,
                    "rank_key": list(_rank(attempt)),
                    "fingerprint": attempt.fingerprint,
                    "patched_ast_fingerprint": (
                        attempt.validation_result.patched_ast_fingerprint
                        if attempt.validation_result else None
                    ),
                    "repair_status": attempt.repair_result.status,
                    "model": attempt.repair_result.model,
                    "prompt_tokens": attempt.repair_result.prompt_tokens,
                    "completion_tokens": attempt.repair_result.completion_tokens,
                    "candidate_explanation": (
                        attempt.repair_result.candidate.explanation
                        if attempt.repair_result.candidate else None
                    ),
                    "validation_status": (
                        attempt.validation_result.status
                        if attempt.validation_result else None
                    ),
                    "public_tests_passed": (
                        attempt.validation_result.public_tests_passed
                        if attempt.validation_result else False
                    ),
                    "public_failure_count": (
                        len(attempt.validation_result.public_test_result.testfailures)
                        if attempt.validation_result and attempt.validation_result.public_test_result
                        else None
                    ),
                    "review": (
                        attempt.validation_result.review.to_dict()
                        if attempt.validation_result and attempt.validation_result.review
                        else None
                    ),
                    "reason": (
                        attempt.validation_result.reason
                        if attempt.validation_result else attempt.repair_result.explanation
                    ),
                    "patch": attempt_patches.get(attempt.number),
                }
                for attempt in run.attempts
            ],
        },
        "schedule": [
            {"step": event.step, "evidence": event.evidence,
             "action": event.action, "reason": event.reason}
            for event in run.schedule
        ],
        "structured_memory": run.memory_snapshot,
        "evidence_chain": _evidence_chain(run, patch_filename),
        "models": {
            "locator": locator.model,
            "repair": repair.model,
        },
        "model_calls": run.model_calls,
        "token_usage": {
            "locator_prompt": (
                sum(item.prompt_tokens or 0 for item in locator_rounds)
                if any(item.prompt_tokens is not None for item in locator_rounds) else None
            ),
            "locator_completion": (
                sum(item.completion_tokens or 0 for item in locator_rounds)
                if any(item.completion_tokens is not None for item in locator_rounds) else None
            ),
            "repair_prompt": repair.prompt_tokens,
            "repair_completion": repair.completion_tokens,
            "total_prompt": run.total_prompt_tokens,
            "total_completion": run.total_completion_tokens,
        },
        "artifacts": {
            "candidate_patch": patch_filename,
            "attempt_patches": attempt_patches,
            "markdown_report": "report.md",
        },
        "not_verified": [
            "Hidden tests were not run in this workflow.",
            "Public tests passing does not establish absence of regressions.",
            *(["Only one candidate was generated; no alternative patches were compared."]
              if len(run.attempts) <= 1 else []),
            "Patch-policy checks are heuristic and do not prove the absence of speculative fixes.",
        ],
    }


def save_repair_run(
    *,
    task: TaskSpec,
    run: RepairRun,
    output_root: Path,
) -> SavedRepairArtifacts:
    """Keep the report and any applied diff outside the temporary workspace."""

    created_at = datetime.now(timezone.utc)
    run_id = f"{created_at:%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    run_directory = (output_root.resolve() / run_id)
    run_directory.mkdir(parents=True, exist_ok=False)

    validation = run.validation_result
    diff = validation.diff if validation and validation.status != "rejected" else ""
    patch_path = run_directory / "candidate.patch" if diff else None
    if patch_path is not None:
        patch_path.write_text(diff, encoding="utf-8", newline="\n")

    attempt_patch_filenames: dict[int, str] = {}
    for attempt in run.attempts:
        attempt_diff = attempt.validation_result.diff if attempt.validation_result else ""
        if not attempt_diff:
            continue
        filename = f"candidate_{attempt.number:02d}.patch"
        (run_directory / filename).write_text(
            attempt_diff, encoding="utf-8", newline="\n"
        )
        attempt_patch_filenames[attempt.number] = filename

    report = build_repair_report(
        task=task,
        run=run,
        run_id=run_id,
        created_at_utc=created_at.isoformat(),
        patch_filename=patch_path.name if patch_path else None,
        attempt_patch_filenames=attempt_patch_filenames,
    )
    report_path = run_directory / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_path = run_directory / "report.md"
    markdown_path.write_text(
        render_repair_markdown(report), encoding="utf-8", newline="\n"
    )
    return SavedRepairArtifacts(
        run_directory=run_directory,
        report_path=report_path,
        patch_path=patch_path,
        markdown_path=markdown_path,
    )
