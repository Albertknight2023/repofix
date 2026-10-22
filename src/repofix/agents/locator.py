"""The first RepoFix agent: evidence-grounded root-cause localization."""

import json
import re
import ast
import textwrap
import tokenize
from collections import Counter
from importlib.resources import files
from pathlib import Path

from repofix.agents.models import BugHypothesis, EvidenceItem, LocatorResult
from repofix.analysis import (
    ContextBundle,
    SourceIndex,
    TracebackLocation,
    locate_test_failure,
)
from repofix.analysis.models import SourceSymbol
from repofix.execution.models import TestFailure, TestRunResult, TestRunStatus
from repofix.llm import ChatMessage, ModelClient, ModelResponse


MAX_ISSUE_CHARACTERS = 4000
MAX_FAILURE_CHARACTERS = 2000
MAX_SOURCE_CHARACTERS = 6000
MAX_SOURCE_LINES = 60


def _is_test_file(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_")


def _source_excerpt(location: TracebackLocation) -> str:
    """Read a bounded, line-numbered excerpt from an indexed source file."""

    symbol = location.symbol
    with tokenize.open(location.module.file_path) as source_file:
        lines = source_file.readlines()

    if symbol.end_line - symbol.start_line + 1 <= MAX_SOURCE_LINES:
        start_line = symbol.start_line
        end_line = symbol.end_line
    else:
        start_line = max(symbol.start_line, location.frame.line_number - 25)
        end_line = min(symbol.end_line, start_line + MAX_SOURCE_LINES - 1)

    numbered_lines = [
        f"{line_number:4}: {lines[line_number - 1].rstrip()}"
        for line_number in range(start_line, end_line + 1)
    ]
    return "\n".join(numbered_lines)[:MAX_SOURCE_CHARACTERS]


def _focused_context_excerpt(symbol: SourceSymbol, failure: TestFailure) -> str:
    """Show relevant executable lines when an assertion has no production frame.

    The public test's failing source line supplies search terms. A long
    docstring must not consume the entire source-evidence budget.
    """

    with tokenize.open(symbol.file_path) as source_file:
        lines = source_file.readlines()
    if symbol.end_line - symbol.start_line + 1 <= MAX_SOURCE_LINES:
        start = symbol.start_line
        end = symbol.end_line
        return "\n".join(
            f"{number:4}: {lines[number - 1].rstrip()}"
            for number in range(start, end + 1)
        )[:MAX_SOURCE_CHARACTERS]

    body_start = symbol.start_line + 1
    try:
        parsed = ast.parse(textwrap.dedent(symbol.source_code))
        definition = parsed.body[0]
        if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first_statement = definition.body[0]
            if (
                isinstance(first_statement, ast.Expr)
                and isinstance(first_statement.value, ast.Constant)
                and isinstance(first_statement.value.value, str)
            ):
                body_start = symbol.start_line + first_statement.end_lineno
    except (SyntaxError, IndexError, AttributeError):
        pass

    test_source = "\n".join(
        frame.source_line or ""
        for frame in failure.traceback_frames
        if _is_test_file(frame.file_path)
    )
    terms = {
        term for term in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", test_source)
        if len(term) >= 4 and term not in {"assert", "None", "True", "False", symbol.name}
    }
    body_lines = lines[body_start - 1:symbol.end_line]
    frequencies = Counter(
        term for line in body_lines for term in terms
        if re.search(rf"\b{re.escape(term)}\b", line)
    )
    anchor = body_start
    best_score = 0.0
    for number in range(body_start, symbol.end_line + 1):
        line = lines[number - 1]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        score = sum(
            1.0 / frequencies[term]
            for term in terms
            if frequencies[term] and re.search(rf"\b{re.escape(term)}\b", line)
        )
        if score > best_score:
            anchor, best_score = number, score

    window_size = MAX_SOURCE_LINES - 1
    # Keep more preceding code than trailing code: conditions and assignments
    # immediately before the most relevant line usually explain the failure.
    start = max(body_start, anchor - (window_size * 2) // 3)
    end = min(symbol.end_line, start + window_size - 1)
    start = max(body_start, end - window_size + 1)
    numbered = [f"{symbol.start_line:4}: {lines[symbol.start_line - 1].rstrip()}"]
    numbered.extend(
        f"{number:4}: {lines[number - 1].rstrip()}"
        for number in range(start, end + 1)
        if number != symbol.start_line
    )
    return "\n".join(numbered)[:MAX_SOURCE_CHARACTERS]


def build_diagnostic_evidence(
    source_index: SourceIndex,
    test_result: TestRunResult,
    related_context: ContextBundle | None = None,
) -> tuple[EvidenceItem, ...]:
    """Select public test failures and repository-owned AST locations."""

    if test_result.status is not TestRunStatus.FAILED:
        raise ValueError("locator agent requires a failed public test run")
    if source_index.root_path != test_result.working_path.resolve():
        raise ValueError("source index and test result must use one workspace")

    evidence: list[EvidenceItem] = []
    source_count = 0
    test_count = 0
    location_count = 0
    seen_source: set[tuple[Path, str]] = set()

    for failure_number, failure in enumerate(test_result.testfailures[:3], start=1):
        evidence.append(
            EvidenceItem(
                evidence_id=f"F{failure_number}",
                kind="failure",
                content=(
                    f"test_id: {failure.test_id}\n"
                    f"phase: {failure.phase}\n"
                    f"exception_type: {failure.exception_type}\n"
                    f"message: {failure.message[:MAX_FAILURE_CHARACTERS]}"
                ),
            )
        )

        locations = locate_test_failure(source_index, failure)
        production_locations = [
            location for location in locations
            if not _is_test_file(location.module.relative_path)
        ]
        test_locations = [
            location for location in locations
            if _is_test_file(location.module.relative_path)
        ]
        # The deepest frame is often only the point where a bad argument
        # finally crashes. Reserve evidence space for the first production
        # caller reached from the failed test as well.
        ordered_locations = (
            production_locations[:1]
            + production_locations[-1:]
            + production_locations[1:-1]
            + test_locations[:1]
        )
        for location in ordered_locations:
            relative_path = location.module.relative_path
            key = (relative_path, location.symbol.qualified_name)
            if key in seen_source:
                continue

            if _is_test_file(relative_path):
                if test_count >= 1:
                    continue
                test_count += 1
                evidence.append(
                    EvidenceItem(
                        evidence_id=f"T{test_count}",
                        kind="public_test",
                        file_path=relative_path,
                        line_number=location.frame.line_number,
                        symbol_name=location.symbol.qualified_name,
                        content=_source_excerpt(location),
                    )
                )
                continue

            if source_count >= 3:
                continue

            seen_source.add(key)
            source_count += 1
            location_count += 1
            evidence.extend(
                (
                    EvidenceItem(
                        evidence_id=f"L{location_count}",
                        kind="traceback_location",
                        file_path=relative_path,
                        line_number=location.frame.line_number,
                        symbol_name=location.symbol.qualified_name,
                        content=(
                            f"{relative_path.as_posix()}:{location.frame.line_number} "
                            f"in {location.symbol.qualified_name}; "
                            f"source_line={location.frame.source_line or ''}"
                        ),
                    ),
                    EvidenceItem(
                        evidence_id=f"S{source_count}",
                        kind="source",
                        file_path=relative_path,
                        line_number=location.frame.line_number,
                        symbol_name=location.symbol.qualified_name,
                        content=_source_excerpt(location),
                    ),
                )
            )
            if (
                production_locations
                and location == production_locations[-1]
                and location.symbol.parent_qualified_name
                and source_count < 3
            ):
                parent = next(
                    (
                        symbol for symbol in source_index.symbols
                        if symbol.file_path == location.symbol.file_path
                        and symbol.qualified_name == location.symbol.parent_qualified_name
                        and symbol.is_function
                    ),
                    None,
                )
                if parent is not None:
                    parent_key = (relative_path, parent.qualified_name)
                    if parent_key not in seen_source:
                        seen_source.add(parent_key)
                        source_count += 1
                        evidence.append(
                            EvidenceItem(
                                evidence_id=f"S{source_count}",
                                kind="source",
                                file_path=relative_path,
                                line_number=parent.start_line,
                                symbol_name=parent.qualified_name,
                                content=_source_excerpt(TracebackLocation(
                                    frame=location.frame,
                                    module=location.module,
                                    symbol=parent,
                                    exact_name_match=False,
                                )),
                            )
                        )

    if source_count == 0 and related_context is not None:
        target = related_context.target
        source_count += 1
        excerpt = _focused_context_excerpt(target, test_result.testfailures[0])
        evidence.append(
            EvidenceItem(
                evidence_id="S1",
                kind="source",
                file_path=target.relative_path,
                line_number=target.start_line,
                symbol_name=target.qualified_name,
                content=excerpt,
            )
        )

    # A traceback can stop in an accessor even when the root cause is an
    # earlier initializer branch. If the call-graph context already contains
    # the class initializer, make that checked source an eligible repair
    # target rather than only a non-editable call-context hint.
    if related_context is not None and source_count < 3:
        for snippet in related_context.related:
            if snippet.relation != "class_initializer":
                continue
            key = (snippet.file_path, snippet.symbol_name)
            if key in seen_source:
                continue
            seen_source.add(key)
            source_count += 1
            evidence.append(
                EvidenceItem(
                    evidence_id=f"S{source_count}",
                    kind="source",
                    file_path=snippet.file_path,
                    line_number=snippet.call_line,
                    symbol_name=snippet.symbol_name,
                    content=snippet.source_code[:MAX_SOURCE_CHARACTERS],
                )
            )
            break

    if (
        related_context is not None
        and related_context.selection_rule == "deepest production traceback frame"
    ):
        known_symbols = {
            (item.file_path, item.symbol_name)
            for item in evidence
            if item.file_path is not None and item.symbol_name is not None
        }
        context_number = 0
        for snippet in related_context.related:
            key = (snippet.file_path, snippet.symbol_name)
            if key in known_symbols:
                continue
            known_symbols.add(key)
            context_number += 1
            evidence.append(
                EvidenceItem(
                    evidence_id=f"C{context_number}",
                    kind="call_context",
                    file_path=snippet.file_path,
                    line_number=snippet.call_line,
                    symbol_name=snippet.symbol_name,
                    content=(
                        f"relation: {snippet.relation}; "
                        f"call_line: {snippet.call_line}\n"
                        f"{snippet.source_code}"
                    ),
                )
            )

    return tuple(evidence)


def _required_text(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"locator output needs nonempty {field_name}")
    return value.strip()


def parse_locator_response(
    response: ModelResponse,
    evidence: tuple[EvidenceItem, ...],
) -> LocatorResult:
    try:
        payload = json.loads(response.content)
    except json.JSONDecodeError as error:
        raise ValueError("locator model did not return valid JSON") from error

    if not isinstance(payload, dict):
        raise ValueError("locator model output must be a JSON object")

    status = payload.get("status")
    if status == "insufficient_evidence":
        return LocatorResult(
            status=status,
            hypothesis=None,
            evidence=evidence,
            explanation=_required_text(payload, "explanation"),
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )

    if status != "hypothesis":
        raise ValueError("locator status must be hypothesis or insufficient_evidence")

    summary = _required_text(payload, "summary")
    root_cause = _required_text(payload, "root_cause")
    target_file_text = _required_text(payload, "target_file")
    target_symbol = _required_text(payload, "target_symbol")
    confidence = payload.get("confidence")
    if type(confidence) not in {int, float} or not 0 <= confidence <= 1:
        raise ValueError("locator confidence must be a number from 0 to 1")

    target_file = Path(target_file_text)
    if (
        target_file.is_absolute()
        or ".." in target_file.parts
        or target_file.as_posix() != target_file_text.replace("\\", "/")
    ):
        raise ValueError("locator target_file must be a repository-relative path")

    evidence_ids = payload.get("evidence_ids")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or any(not isinstance(item, str) for item in evidence_ids)
        or len(evidence_ids) != len(set(evidence_ids))
    ):
        raise ValueError("locator evidence_ids must be unique evidence IDs")

    available_ids = {item.evidence_id for item in evidence}
    if not set(evidence_ids).issubset(available_ids):
        raise ValueError("locator cited an unknown evidence ID")

    matching_source_ids = {
        item.evidence_id
        for item in evidence
        if item.kind == "source"
        and item.file_path == target_file
        and item.symbol_name == target_symbol
    }
    if not matching_source_ids:
        qualified_matches = {
            item.symbol_name
            for item in evidence
            if item.kind == "source"
            and item.file_path == target_file
            and item.symbol_name is not None
            and item.symbol_name.rsplit(".", 1)[-1] == target_symbol
        }
        if len(qualified_matches) == 1:
            target_symbol = qualified_matches.pop()
            matching_source_ids = {
                item.evidence_id
                for item in evidence
                if item.kind == "source"
                and item.file_path == target_file
                and item.symbol_name == target_symbol
            }
    if not matching_source_ids or not matching_source_ids.intersection(evidence_ids):
        raise ValueError("locator target must cite a matching source symbol")

    next_step_value = payload.get("next_step")
    if isinstance(next_step_value, str) and next_step_value.strip():
        next_step = next_step_value.strip()
        explanation = "This is an unverified root-cause hypothesis."
    else:
        # A missing hand-off sentence is not a missing source citation. Derive
        # only the checked target, never a new claim about the root cause.
        next_step = f"Inspect {target_file.as_posix()}::{target_symbol}."
        explanation = "Unverified hypothesis; next_step was derived from the checked target."

    hypothesis = BugHypothesis(
        summary=summary,
        root_cause=root_cause,
        target_file=target_file,
        target_symbol=target_symbol,
        confidence=float(confidence),
        evidence_ids=tuple(evidence_ids),
        next_step=next_step,
    )
    return LocatorResult(
        status=status,
        hypothesis=hypothesis,
        evidence=evidence,
        explanation=explanation,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )


class LocatorAgent:
    """Ask one model to explain a failure using only checked evidence."""

    def __init__(self, model_client: ModelClient) -> None:
        self._model_client = model_client

    def diagnose(
        self,
        *,
        issue: str,
        test_result: TestRunResult,
        source_index: SourceIndex,
        related_context: ContextBundle | None = None,
        retry_feedback: str | None = None,
    ) -> LocatorResult:
        evidence = build_diagnostic_evidence(source_index, test_result, related_context)
        if not any(item.kind == "source" for item in evidence):
            return LocatorResult(
                status="insufficient_evidence",
                hypothesis=None,
                evidence=evidence,
                explanation="The public-test traceback did not map to a source function.",
                model="none",
            )

        prompt = (
            files("repofix.agents.prompts")
            .joinpath("locator.md")
            .read_text(encoding="utf-8")
        )
        user_payload = {
            "issue": issue[:MAX_ISSUE_CHARACTERS],
            "eligible_targets": [
                {
                    "source_evidence_id": item.evidence_id,
                    "target_file": item.file_path.as_posix(),
                    "target_symbol": item.symbol_name,
                }
                for item in evidence
                if item.kind == "source"
                and item.file_path is not None
                and item.symbol_name is not None
            ],
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "kind": item.kind,
                    "file_path": (
                        item.file_path.as_posix()
                        if item.file_path is not None
                        else None
                    ),
                    "line_number": item.line_number,
                    "symbol_name": item.symbol_name,
                    "content": item.content,
                }
                for item in evidence
            ],
        }
        if retry_feedback:
            user_payload["previous_submission_feedback"] = retry_feedback[:500]
        response = self._model_client.complete(
            (
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content=json.dumps(user_payload, ensure_ascii=False),
                ),
            )
        )
        return parse_locator_response(response, evidence)
