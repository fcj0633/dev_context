from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ANSWER_DEPTHS = ("brief", "standard", "detailed")

# Evidence sources are named the way the retrieval layer names them ("DOCUMENT",
# not "DOC") so a requirement can be handed to a search without translation.
EVIDENCE_SOURCES = ("CODE", "DOCUMENT")


@dataclass(frozen=True, slots=True)
class SubQuestion:
    id: str
    question: str
    purpose: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    original_query: str
    intent_summary: str
    sub_questions: tuple[SubQuestion, ...]
    answer_depth: str
    decision_source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "intent_summary": self.intent_summary,
            "answer_depth": self.answer_depth,
            "decision_source": self.decision_source,
            "sub_questions": [item.to_dict() for item in self.sub_questions],
        }


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    """What one sub-question needs to look up, and which sources can answer it."""

    id: str
    sub_question_id: str
    description: str
    preferred_sources: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sub_question_id": self.sub_question_id,
            "description": self.description,
            "preferred_sources": list(self.preferred_sources),
        }


@dataclass(frozen=True, slots=True)
class EvidencePlan:
    requirements: tuple[EvidenceRequirement, ...]
    decision_source: str = "llm"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_source": self.decision_source,
            "requirements": [item.to_dict() for item in self.requirements],
        }
