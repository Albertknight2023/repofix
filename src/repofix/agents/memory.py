"""Small, public-only memory scoped to one repair run."""

from dataclasses import dataclass, field

from repofix.agents.models import LocatorResult
from repofix.agents.repair import RepairResult
from repofix.analysis import ContextBundle, SourceIndex
from repofix.execution.models import TestRunResult
from repofix.repair import ValidationResult


@dataclass(slots=True)
class RunMemory:
    """Separate stable project facts, bug facts and changing attempt facts."""

    project: dict[str, object]
    bug: dict[str, object]
    iterations: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def create(
        cls, *, issue: str, source_index: SourceIndex,
        context: ContextBundle | None, locator: LocatorResult,
        initial_result: TestRunResult,
    ) -> "RunMemory":
        functions = [item for item in source_index.symbols if item.is_function]
        return cls(
            project={
                "module_count": len(source_index.modules),
                "function_count": len(functions),
                "parse_issue_count": len(source_index.issues),
                "target_symbol": (
                    locator.hypothesis.target_symbol if locator.hypothesis else None
                ),
                "related_symbols": [
                    item.symbol_name for item in (context.related if context else ())
                ][:4],
            },
            bug={
                "issue": issue[:1000],
                "failure_signatures": [
                    {"test_id": item.test_id[:200], "exception_type": item.exception_type[:100]}
                    for item in initial_result.testfailures[:3]
                ],
                "hypothesis": (
                    locator.hypothesis.summary[:500] if locator.hypothesis else None
                ),
                "supporting_evidence_ids": (
                    list(locator.hypothesis.evidence_ids) if locator.hypothesis else []
                ),
            },
        )

    def record(
        self, *, number: int, strategy: str, fingerprint: str | None,
        repair: RepairResult, validation: ValidationResult | None,
    ) -> None:
        result = validation.public_test_result if validation else None
        review = validation.review if validation else None
        self.iterations.append({
            "number": number,
            "strategy": strategy,
            "fingerprint": fingerprint,
            "repair_status": repair.status,
            "validation_status": validation.status if validation else None,
            "failed_test_ids": [
                failure.test_id[:200] for failure in (result.testfailures[:5] if result else ())
            ],
            "review_codes": [finding.code for finding in (review.findings if review else ())],
        })

    def relocalize(
        self, *, locator: LocatorResult, result: TestRunResult,
        context: ContextBundle | None,
    ) -> None:
        """Keep the new hypothesis separate from the failed candidate's history."""

        hypothesis = locator.hypothesis
        self.project["target_symbol"] = hypothesis.target_symbol if hypothesis else None
        self.project["related_symbols"] = [
            item.symbol_name for item in (context.related if context else ())
        ][:4]
        self.bug["hypothesis"] = hypothesis.summary[:500] if hypothesis else None
        self.bug["supporting_evidence_ids"] = (
            list(hypothesis.evidence_ids) if hypothesis else []
        )
        self.bug["failure_signatures"] = [
            {"test_id": item.test_id[:200], "exception_type": item.exception_type[:100]}
            for item in result.testfailures[:3]
        ]
        self.bug["relocalized_after_attempt"] = len(self.iterations)

    def snapshot(self) -> dict[str, object]:
        return {
            "scope": "single_run",
            "project": dict(self.project),
            "bug": dict(self.bug),
            "iterations": [dict(item) for item in self.iterations],
        }
