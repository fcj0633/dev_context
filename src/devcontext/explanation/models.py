from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from devcontext.models import EvidenceConflict
# The depth vocabulary belongs to the request contract; importing it here keeps
# the prompt, the validator and the CLI from drifting apart.
from devcontext.request import (  # noqa: F401  (re-exported for the prompt builder)
    ANSWER_DEPTHS,
    EXPLAIN_MODES,
    TEACH_DEPTHS,
    depths_for,
)


# What kind of work a section does for the reader. Ordering these is the whole
# point of planning separately from retrieval: the investigation order is not the
# teaching order, and this is where that is expressed.
SECTION_TYPES = (
    "DIRECT_ANSWER",
    "PROBLEM_SETUP",
    "MENTAL_MODEL",
    "CONCEPT",
    "EXECUTION_FLOW",
    "MECHANISM",
    "CAUSE",
    "DESIGN_REASON",
    "COMPARISON",
    "FAILURE_SCENARIO",
    "TRADEOFF",
    "BOUNDARY",
    "MISCONCEPTION",
    "SUMMARY",
)

TEACHING_DEVICES = (
    "NONE",
    "HYPOTHETICAL_EXAMPLE",
    "COUNTER_EXAMPLE",
    "A_B_REQUEST_TRACE",
    "ANALOGY",
    "SMALL_FLOW_DIAGRAM",
    "TABLE",
    "PSEUDOCODE",
)

# Four layers, not three: PROJECT_INFERENCE sits between a fact and a general
# concept, and it is the layer that lets the answer reason past the evidence
# without pretending the reasoning is itself evidence.
CLAIM_TYPES = (
    "PROJECT_FACT",
    "PROJECT_INFERENCE",
    "GENERAL_CONCEPT",
    "ILLUSTRATIVE_EXAMPLE",
)
CONFIDENCES = ("CONFIRMED", "PARTIAL", "UNVERIFIED")

PRIMARY_STRATEGIES = (
    "PROBLEM_SOLUTION",
    "CONCEPT_BUILDUP",
    "EXECUTION_FLOW",
    "CAUSE_EFFECT",
    "LAYERED_ARCHITECTURE",
    "FAILURE_ANALYSIS",
    "TRADEOFF",
    "COMPARISON",
    "NEGATIVE_CORRECTION",
    "LOCATION_ONLY",
)

# `deep` exists because the old fixed ceilings were the binding constraint on a
# hard question, not the model's window. It is only meaningful on the teach path,
# so an explanation plan - which only the teach path produces - uses the full set.
DEPTHS = TEACH_DEPTHS
EXPLAIN_DEPTHS = ANSWER_DEPTHS


@dataclass(frozen=True, slots=True)
class ClaimPlan:
    """One assertion a section intends to make, and what licenses it."""

    claim_goal: str
    claim_type: str
    evidence_labels: tuple[str, ...]
    confidence: str
    # Required when the claim cannot be settled from the evidence alone. A claim
    # with assumptions is asserted conditionally - "if X then Y" - never as a
    # flat statement about the project.
    assumptions: tuple[str, ...] = ()
    conditional: bool = False

    def __post_init__(self) -> None:
        if self.claim_type not in CLAIM_TYPES:
            raise ValueError(f"invalid claim_type: {self.claim_type}")
        if self.confidence not in CONFIDENCES:
            raise ValueError(f"invalid confidence: {self.confidence}")
        if self.claim_type == "PROJECT_FACT" and not self.evidence_labels:
            raise ValueError("PROJECT_FACT requires project evidence")
        if self.claim_type == "ILLUSTRATIVE_EXAMPLE":
            if not self.conditional or not self.assumptions:
                raise ValueError(
                    "ILLUSTRATIVE_EXAMPLE must be conditional and state its assumptions"
                )
        if self.conditional and not self.assumptions:
            raise ValueError("a conditional claim must state its assumptions")
        if self.conditional and self.confidence == "CONFIRMED":
            raise ValueError("a conditional claim cannot be CONFIRMED")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_labels"] = list(self.evidence_labels)
        value["assumptions"] = list(self.assumptions)
        return value


@dataclass(frozen=True, slots=True)
class ExplanationSection:
    id: str
    title: str
    section_type: str
    teaching_goal: str
    key_points: tuple[str, ...]
    claim_plans: tuple[ClaimPlan, ...]
    evidence_labels: tuple[str, ...]
    teaching_devices: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    evidence_state: str = "CONFIRMED"
    target_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.section_type not in SECTION_TYPES:
            raise ValueError(f"invalid section_type: {self.section_type}")
        if self.evidence_state not in CONFIDENCES:
            raise ValueError(f"invalid evidence_state: {self.evidence_state}")
        for device in self.teaching_devices:
            if device not in TEACHING_DEVICES:
                raise ValueError(f"invalid teaching_device: {device}")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["key_points"] = list(self.key_points)
        value["claim_plans"] = [item.to_dict() for item in self.claim_plans]
        value["evidence_labels"] = list(self.evidence_labels)
        value["teaching_devices"] = list(self.teaching_devices)
        value["depends_on"] = list(self.depends_on)
        return value


@dataclass(frozen=True, slots=True)
class ExplanationPlan:
    """What the reader should end up understanding, and in what order.

    Distinct from the evidence plan: that says which facts are required, this
    says how a person should be walked through them.
    """

    answer_goal: str
    direct_answer: str
    audience_model: str
    core_mental_model: str
    primary_strategy: str
    sections: tuple[ExplanationSection, ...]
    answer_depth: str = "standard"
    secondary_strategies: tuple[str, ...] = ()
    prerequisite_concepts: tuple[str, ...] = ()
    likely_misconceptions: tuple[str, ...] = ()
    unresolved_gaps: tuple[str, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()
    decision_source: str = "llm"

    def __post_init__(self) -> None:
        if self.primary_strategy not in PRIMARY_STRATEGIES:
            raise ValueError(f"invalid primary_strategy: {self.primary_strategy}")
        for strategy in self.secondary_strategies:
            if strategy not in PRIMARY_STRATEGIES:
                raise ValueError(f"invalid secondary_strategy: {strategy}")
        if self.answer_depth not in DEPTHS:
            raise ValueError(f"invalid answer_depth: {self.answer_depth}")
        if not self.core_mental_model.strip():
            raise ValueError("an explanation plan must state a core mental model")

    @property
    def evidence_labels(self) -> tuple[str, ...]:
        seen: list[str] = []
        for section in self.sections:
            for label in section.evidence_labels:
                if label not in seen:
                    seen.append(label)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_goal": self.answer_goal,
            "direct_answer": self.direct_answer,
            "audience_model": self.audience_model,
            "core_mental_model": self.core_mental_model,
            "primary_strategy": self.primary_strategy,
            "secondary_strategies": list(self.secondary_strategies),
            "prerequisite_concepts": list(self.prerequisite_concepts),
            "likely_misconceptions": list(self.likely_misconceptions),
            "sections": [item.to_dict() for item in self.sections],
            "unresolved_gaps": list(self.unresolved_gaps),
            "conflicts": [item.to_dict() for item in self.conflicts],
            "answer_depth": self.answer_depth,
            "decision_source": self.decision_source,
        }


@dataclass(frozen=True, slots=True)
class DraftSection:
    section_id: str
    title: str
    text_with_citations: str
    used_citations: tuple[str, ...] = ()
    invalid_citations: tuple[str, ...] = ()
    evidence_state: str = "CONFIRMED"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["used_citations"] = list(self.used_citations)
        value["invalid_citations"] = list(self.invalid_citations)
        return value


@dataclass(slots=True)
class TeachingAnswerPlanBundle:
    """Carrier for the teach path's intermediate artefacts, for tracing."""

    explanation_plan: ExplanationPlan
    section_drafts: list[DraftSection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "explanation_plan": self.explanation_plan.to_dict(),
            "section_drafts": [item.to_dict() for item in self.section_drafts],
        }
