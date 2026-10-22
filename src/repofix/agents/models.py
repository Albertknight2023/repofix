"""Structured evidence and hypotheses produced by the locator agent."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    evidence_id: str
    kind: str
    content: str
    file_path: Path | None = None
    line_number: int | None = None
    symbol_name: str | None = None


@dataclass(frozen=True, slots=True)
class BugHypothesis:
    """An unverified explanation of one observed test failure."""

    summary: str
    root_cause: str
    target_file: Path
    target_symbol: str
    confidence: float
    evidence_ids: tuple[str, ...]
    next_step: str


@dataclass(frozen=True, slots=True)
class LocatorResult:
    """One locator invocation, including its auditable input evidence."""

    status: str
    hypothesis: BugHypothesis | None
    evidence: tuple[EvidenceItem, ...]
    explanation: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
