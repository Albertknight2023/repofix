"""One-call repair baseline using the same checked public evidence."""

import json
from importlib.resources import files

from repofix.agents.locator import (
    MAX_ISSUE_CHARACTERS,
    build_diagnostic_evidence,
    parse_locator_response,
)
from repofix.agents.models import LocatorResult
from repofix.agents.repair import RepairResult, parse_repair_response
from repofix.analysis import ContextBundle, SourceIndex
from repofix.execution.models import TestRunResult
from repofix.llm import ChatMessage, ModelClient, ModelResponse


class DirectRepairAgent:
    """Diagnose and propose a patch in one model invocation, without tools."""

    def __init__(self, model_client: ModelClient) -> None:
        self._model_client = model_client

    def propose(
        self,
        *,
        issue: str,
        test_result: TestRunResult,
        source_index: SourceIndex,
        related_context: ContextBundle | None,
        retry_feedback: str | None = None,
    ) -> tuple[LocatorResult, RepairResult]:
        evidence = build_diagnostic_evidence(
            source_index, test_result, related_context
        )
        if not any(item.kind == "source" for item in evidence):
            locator = LocatorResult(
                status="insufficient_evidence",
                hypothesis=None,
                evidence=evidence,
                explanation="The public-test traceback did not map to a source function.",
                model="none",
            )
            return locator, RepairResult(
                status="no_patch", candidate=None,
                explanation="No checked source function is available for repair.",
                model="none", prompt_tokens=None, completion_tokens=None,
            )

        prompt = (
            files("repofix.agents.prompts")
            .joinpath("direct.md")
            .read_text(encoding="utf-8")
        )
        payload = {
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
                    "file_path": item.file_path.as_posix() if item.file_path else None,
                    "line_number": item.line_number,
                    "symbol_name": item.symbol_name,
                    "content": item.content,
                }
                for item in evidence
            ],
        }
        if retry_feedback:
            payload["previous_submission_feedback"] = retry_feedback[:500]
        response = self._model_client.complete((
            ChatMessage(role="system", content=prompt),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
        ))
        try:
            answer = json.loads(response.content)
        except json.JSONDecodeError as error:
            raise ValueError("direct repair model did not return valid JSON") from error
        if not isinstance(answer, dict):
            raise ValueError("direct repair output must be a JSON object")
        diagnosis = answer.get("diagnosis")
        repair = answer.get("repair")
        if not isinstance(diagnosis, dict) or not isinstance(repair, dict):
            raise ValueError("direct repair output needs diagnosis and repair objects")

        locator = parse_locator_response(
            ModelResponse(
                content=json.dumps(diagnosis, ensure_ascii=False),
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            ),
            evidence,
        )
        if locator.hypothesis is None:
            if repair.get("status") != "no_patch":
                raise ValueError("direct repair cannot patch without a verified diagnosis")
            explanation = repair.get("explanation")
            if not isinstance(explanation, str) or not explanation.strip():
                raise ValueError("direct no_patch output needs an explanation")
            return locator, RepairResult(
                status="no_patch", candidate=None, explanation=explanation.strip(),
                model=response.model, prompt_tokens=None, completion_tokens=None,
            )
        result = parse_repair_response(
            ModelResponse(
                content=json.dumps(repair, ensure_ascii=False),
                model=response.model,
                # One model call: usage is recorded once on the diagnosis.
                prompt_tokens=None,
                completion_tokens=None,
            ),
            locator_result=locator,
            source_index=source_index,
        )
        return locator, result
