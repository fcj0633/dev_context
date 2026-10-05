from __future__ import annotations

from dataclasses import dataclass, InitVar
from typing import Any


EXPLANATION_STRATEGIES = ("flow", "causal", "comparison", "architecture", "mixed")
IMPORTANCE_LEVELS = ("CORE", "SUPPORTING")
TEMPORAL_SCOPES = ("CURRENT", "HISTORY", "FUTURE", "ANY")

# Evidence sources are named the way the retrieval layer names them ("DOCUMENT",
# not "DOC") so a requirement can be handed to a search without translation.
EVIDENCE_SOURCES = ("CODE", "DOCUMENT")


@dataclass(frozen=True, slots=True)
class SubQuestion:
    """Deprecated compatibility model; new workflows use EvidenceRequirement."""

    id: str
    question: str
    purpose: str
    evidence_description: str
    preferred_sources: tuple[str, ...]
    retrieval_query: str = ""
    importance: str = "CORE"
    temporal_scope: str = "CURRENT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "purpose": self.purpose,
            "evidence_description": self.evidence_description,
            "preferred_sources": list(self.preferred_sources),
            "retrieval_query": self.retrieval_query,
            "importance": self.importance,
            "temporal_scope": self.temporal_scope,
        }


@dataclass(frozen=True, slots=True)
class QuestionPlan:
    """Deprecated compatibility model; new workflows use EvidencePlan."""

    original_query: str
    intent_summary: str
    sub_questions: tuple[SubQuestion, ...]
    answer_depth: InitVar[str | None] = None
    decision_source: str = "llm"
    answer_goal: str = ""
    explanation_strategy: str = "mixed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "intent_summary": self.intent_summary,
            "decision_source": self.decision_source,
            "answer_goal": self.answer_goal,
            "explanation_strategy": self.explanation_strategy,
            "sub_questions": [item.to_dict() for item in self.sub_questions],
        }
