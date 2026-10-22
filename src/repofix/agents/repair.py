"""A repair agent that proposes one replacement for an indexed function."""

import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from repofix.agents.models import LocatorResult
from repofix.analysis import ContextBundle, SourceIndex
from repofix.llm import ChatMessage, ModelClient, ModelResponse


@dataclass(frozen=True, slots=True)
class PatchCandidate:
    """A model-proposed function replacement; validation happens separately."""

    target_file: Path
    target_symbol: str
    replacement_source: str
    explanation: str
    evidence_ids: tuple[str, ...]
    old_snippet: str | None = None
    new_snippet: str | None = None


@dataclass(frozen=True, slots=True)
class RepairResult:
    status: str
    candidate: PatchCandidate | None
    explanation: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


def _required_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"repair output needs nonempty {name}")
    return value.strip()


def parse_repair_response(
    response: ModelResponse,
    *,
    locator_result: LocatorResult,
    source_index: SourceIndex,
) -> RepairResult:
    """Validate one model answer against the checked diagnosis and source index."""

    hypothesis = locator_result.hypothesis
    if hypothesis is None:
        raise ValueError("repair answer requires a verified locator hypothesis")
    try:
        answer = json.loads(response.content)
    except json.JSONDecodeError as error:
        raise ValueError("repair model did not return valid JSON") from error
    if not isinstance(answer, dict):
        raise ValueError("repair output must be a JSON object")

    if answer.get("status") == "no_patch":
        return RepairResult(
            status="no_patch",
            candidate=None,
            explanation=_required_string(answer, "explanation"),
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
        )
    if answer.get("status") != "patch":
        raise ValueError("repair status must be patch or no_patch")

    target_file = Path(_required_string(answer, "target_file"))
    target_symbol = _required_string(answer, "target_symbol")
    if target_symbol == hypothesis.target_symbol.rsplit(".", 1)[-1]:
        target_symbol = hypothesis.target_symbol
    if (
        target_file != hypothesis.target_file
        or target_symbol != hypothesis.target_symbol
    ):
        raise ValueError("repair target differs from the verified locator target")
    if not any(
        symbol.relative_path == target_file
        and symbol.qualified_name == target_symbol
        and symbol.is_function
        for symbol in source_index.symbols
    ):
        raise ValueError("repair target is absent from the current index")

    edit_kind = answer.get("edit_kind", "function")
    if edit_kind == "snippet":
        replacement_source = ""
        old_snippet = _required_string(answer, "old_snippet")
        new_snippet = _required_string(answer, "new_snippet")
        if len(old_snippet) > 3000 or len(new_snippet) > 3000:
            raise ValueError("repair snippet exceeds size limit")
    elif edit_kind == "function":
        replacement_source = _required_string(answer, "replacement_source")
        old_snippet = new_snippet = None
        if len(replacement_source) > 20000:
            raise ValueError("repair replacement exceeds size limit")
    else:
        raise ValueError("repair edit_kind must be function or snippet")
    evidence_ids = answer.get("evidence_ids")
    known_ids = {item.evidence_id for item in locator_result.evidence}
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or any(not isinstance(value, str) for value in evidence_ids)
        or len(evidence_ids) != len(set(evidence_ids))
        or not set(evidence_ids).issubset(known_ids)
    ):
        raise ValueError("repair output has invalid evidence IDs")

    return RepairResult(
        status="patch",
        candidate=PatchCandidate(
            target_file=target_file,
            target_symbol=target_symbol,
            replacement_source=replacement_source,
            explanation=_required_string(answer, "explanation"),
            evidence_ids=tuple(evidence_ids),
            old_snippet=old_snippet,
            new_snippet=new_snippet,
        ),
        explanation="Candidate generated; see validation for test results.",
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
    )


class RepairAgent:
    """Ask a model for code without granting it filesystem access."""

    def __init__(self, model_client: ModelClient) -> None:
        self._model_client = model_client

    def propose(
        self,
        *,
        locator_result: LocatorResult,
        source_index: SourceIndex,
        related_context: ContextBundle | None,
        strategy: str | None = None,
        feedback: dict[str, object] | None = None,
        memory: dict[str, object] | None = None,
    ) -> RepairResult:
        hypothesis = locator_result.hypothesis
        if hypothesis is None:
            return RepairResult(
                status="no_patch",
                candidate=None,
                explanation="The locator did not produce a hypothesis.",
                model="none",
                prompt_tokens=None,
                completion_tokens=None,
            )

        targets = [
            symbol for symbol in source_index.symbols
            if symbol.relative_path == hypothesis.target_file
            and symbol.qualified_name == hypothesis.target_symbol
            and symbol.is_function
        ]
        if not targets:
            raise ValueError("locator target is absent from the current index")
        if len(targets) > 1:
            cited_lines = {
                item.line_number for item in locator_result.evidence
                if item.evidence_id in hypothesis.evidence_ids
                and item.kind == "source"
                and item.file_path == hypothesis.target_file
                and item.symbol_name == hypothesis.target_symbol
                and item.line_number is not None
            }
            targets = [
                symbol for symbol in targets
                if any(symbol.contains_line(line) for line in cited_lines)
            ]
            if len(targets) != 1:
                raise ValueError("locator target is ambiguous in the current index")
        target = targets[0]

        prompt = (
            files("repofix.agents.prompts")
            .joinpath("repair.md")
            .read_text(encoding="utf-8")
        )
        payload = {
            "hypothesis": {
                "summary": hypothesis.summary,
                "root_cause": hypothesis.root_cause,
                "target_file": hypothesis.target_file.as_posix(),
                "target_symbol": hypothesis.target_symbol,
                "next_step": hypothesis.next_step,
                "evidence_ids": list(hypothesis.evidence_ids),
            },
            "evidence": [
                {
                    "id": item.evidence_id,
                    "kind": item.kind,
                    "content": item.content,
                }
                for item in locator_result.evidence
            ],
            "target_source": target.source_code[:12000],
            "target_line_count": target.end_line - target.start_line + 1,
            "related_context": [
                {
                    "file_path": item.file_path.as_posix(),
                    "symbol_name": item.symbol_name,
                    "relation": item.relation,
                    "source_code": item.source_code,
                }
                for item in (related_context.related if related_context else ())
            ],
        }
        if strategy is not None:
            payload["repair_strategy"] = strategy
        if feedback is not None:
            payload["public_validation_feedback"] = feedback
        if memory is not None:
            payload["structured_memory"] = memory
        response = self._model_client.complete(
            (
                ChatMessage(role="system", content=prompt),
                ChatMessage(
                    role="user",
                    content=json.dumps(payload, ensure_ascii=False),
                ),
            )
        )

        return parse_repair_response(
            response, locator_result=locator_result, source_index=source_index
        )
