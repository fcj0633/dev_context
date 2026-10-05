from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


from devcontext.models import CONFLICT_RESOLUTIONS, EvidenceConflict

REVIEW_ISSUE_TYPES = (
    "UNSUPPORTED_CLAIM",
    "SOURCE_CONFLICT",
    "MISSING_CORE_POINT",
    "REPETITION",
    "MECHANICAL_STRUCTURE",
    "POOR_ORDER",
    "OVERCLAIM",
    "LENGTH_VIOLATION",
)

# The teaching dimensions. LENGTH_VIOLATION loses its fixed window here: what is
# too long is now "repeats itself" or "does not fit the output budget", not
# "exceeds 5000 characters".
TEACHING_ISSUE_TYPES = (
    "MISSING_MENTAL_MODEL",
    "MISSING_WHY",
    "POOR_SCAFFOLDING",
    "MISLABELED_EXAMPLE",
    "FACT_INFERENCE_CONFUSION",
    "GENERAL_KNOWLEDGE_AS_PROJECT_FACT",
    "SECTION_EVIDENCE_MISMATCH",
    "UNHELPFUL_DETAIL",
    "ABRUPT_TRANSITION",
)


@dataclass(frozen=True, slots=True)
class AnswerSection:
    title: str
    purpose: str
    key_points: tuple[str, ...]
    evidence_labels: tuple[str, ...]
    target_chars: int

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["key_points"] = list(self.key_points)
        value["evidence_labels"] = list(self.evidence_labels)
        return value


@dataclass(frozen=True, slots=True)
class EvidenceConflict:
    topic: str
    evidence_labels: tuple[str, ...]
    resolution: str
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_labels"] = list(self.evidence_labels)
        return value


@dataclass(frozen=True, slots=True)
class AnswerPlan:
    direct_answer: str
    summary_citation_labels: tuple[str, ...]
    explanation_strategy: str
    sections: tuple[AnswerSection, ...]
    unresolved_gaps: tuple[str, ...]
    conflicts: tuple[EvidenceConflict, ...]
    decision_source: str = "llm"
    answer_goal: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "direct_answer": self.direct_answer,
            "summary_citation_labels": list(self.summary_citation_labels),
            "explanation_strategy": self.explanation_strategy,
            "sections": [item.to_dict() for item in self.sections],
            "unresolved_gaps": list(self.unresolved_gaps),
            "conflicts": [item.to_dict() for item in self.conflicts],
            "decision_source": self.decision_source,
            "answer_goal": self.answer_goal,
        }


@dataclass(frozen=True, slots=True)
class GroundedDraft:
    text_with_citations: str
    used_citations: tuple[str, ...]
    invalid_citations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    issue_type: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"issue_type": self.issue_type, "description": self.description}


@dataclass(frozen=True, slots=True)
class ReviewResult:
    accepted: bool
    issues: tuple[ReviewIssue, ...]
    final_answer_with_citations: str
    decision_source: str = "llm"
    # Why the reviewer fell back, so a failed review is diagnosable rather than silent.
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "issues": [item.to_dict() for item in self.issues],
            "final_answer_with_citations": self.final_answer_with_citations,
            "decision_source": self.decision_source,
            "error": self.error,
        }
