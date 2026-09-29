from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from devcontext.agentic.models import EvidenceStatus, MissingAspect, SufficiencyResult
from devcontext.models import ContextBundle
from devcontext.planning import EvidencePlan, EvidenceRequirement


COVERAGE_STATES = ("SATISFIED", "PARTIAL", "MISSING", "UNVERIFIED")
RETRIEVAL_STATES = ("READY", "PARTIAL", "EMPTY")


@dataclass(frozen=True, slots=True)
class SearchAction:
    action_id: str
    requirement_id: str
    round_index: int
    query: str
    source_scope: str
    reason: str
    decision_source: str = "llm"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RequirementCoverage:
    requirement_id: str
    state: str
    evidence_ids: tuple[int, ...]
    missing_criteria: tuple[str, ...]
    reason: str
    decision_source: str
    check_error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in COVERAGE_STATES:
            raise ValueError("invalid coverage state")

    @property
    def satisfied(self) -> bool:
        return self.state == "SATISFIED"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_ids"] = list(self.evidence_ids)
        value["missing_criteria"] = list(self.missing_criteria)
        return value


@dataclass(frozen=True, slots=True)
class CoverageRound:
    round_index: int
    statuses: tuple[RequirementCoverage, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "statuses": [item.to_dict() for item in self.statuses],
        }


@dataclass(frozen=True, slots=True)
class RequirementTrace:
    requirement_id: str
    target: str
    search_history: tuple[SearchAction, ...]
    evidence_ids: tuple[int, ...]
    coverage_history: tuple[RequirementCoverage, ...]
    final_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "target": self.target,
            "search_history": [item.to_dict() for item in self.search_history],
            "evidence_ids": list(self.evidence_ids),
            "coverage_history": [item.to_dict() for item in self.coverage_history],
            "final_state": self.final_state,
        }


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    original_query: str
    evidence_plan: EvidencePlan
    context_bundle: ContextBundle
    requirement_coverage: tuple[RequirementCoverage, ...]
    unresolved_requirements: tuple[str, ...]
    retrieval_state: str
    search_history: tuple[SearchAction, ...]
    coverage_rounds: tuple[CoverageRound, ...] = ()

    def __post_init__(self) -> None:
        if self.retrieval_state not in RETRIEVAL_STATES:
            raise ValueError("invalid retrieval state")

    @property
    def evidence_items(self):
        return tuple(self.context_bundle.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "evidence_plan": self.evidence_plan.to_dict(),
            "evidence_items": [item.to_dict() for item in self.context_bundle.items],
            "requirement_coverage": [
                item.to_dict() for item in self.requirement_coverage
            ],
            "unresolved_requirements": list(self.unresolved_requirements),
            "retrieval_state": self.retrieval_state,
            "search_history": [item.to_dict() for item in self.search_history],
            "coverage_rounds": [item.to_dict() for item in self.coverage_rounds],
        }

    def to_legacy_sufficiency(self) -> SufficiencyResult:
        by_id = {item.id: item for item in self.evidence_plan.requirements}
        missing: list[MissingAspect] = []
        statuses: list[EvidenceStatus] = []
        for coverage in self.requirement_coverage:
            requirement = by_id[coverage.requirement_id]
            statuses.append(
                EvidenceStatus(
                    coverage.requirement_id,
                    coverage.satisfied,
                    coverage.reason,
                )
            )
            if coverage.satisfied:
                continue
            source_type = _legacy_source_type(requirement)
            details = coverage.missing_criteria or (requirement.success_criteria,)
            missing.extend(
                MissingAspect(source_type, detail, requirement.id)
                for detail in details
            )
        return SufficiencyResult(
            enough=self.retrieval_state == "READY",
            missing_aspects=tuple(missing),
            reason=_coverage_reason(self.retrieval_state),
            decision_source=_coverage_decision_source(self.requirement_coverage),
            statuses=tuple(statuses),
        )


def package_state(
    plan: EvidencePlan,
    context: ContextBundle,
    coverage: tuple[RequirementCoverage, ...],
) -> str:
    if not context.items:
        return "EMPTY"
    by_id = {item.requirement_id: item for item in coverage}
    core = [item for item in plan.requirements if item.priority == "CORE"]
    if all(by_id.get(item.id) and by_id[item.id].satisfied for item in core):
        return "READY"
    return "PARTIAL"


def build_requirement_traces(package: EvidencePackage) -> tuple[RequirementTrace, ...]:
    actions_by_id: dict[str, list[SearchAction]] = {}
    for action in package.search_history:
        actions_by_id.setdefault(action.requirement_id, []).append(action)
    history_by_id: dict[str, list[RequirementCoverage]] = {}
    for round_value in package.coverage_rounds:
        for status in round_value.statuses:
            history_by_id.setdefault(status.requirement_id, []).append(status)
    final_by_id = {
        item.requirement_id: item for item in package.requirement_coverage
    }
    return tuple(
        RequirementTrace(
            requirement.id,
            requirement.target,
            tuple(actions_by_id.get(requirement.id, ())),
            final_by_id.get(
                requirement.id,
                RequirementCoverage(
                    requirement.id,
                    "MISSING",
                    (),
                    (requirement.success_criteria,),
                    "未执行覆盖检查",
                    "rules",
                ),
            ).evidence_ids,
            tuple(history_by_id.get(requirement.id, ())),
            final_by_id.get(
                requirement.id,
                RequirementCoverage(
                    requirement.id,
                    "MISSING",
                    (),
                    (requirement.success_criteria,),
                    "未执行覆盖检查",
                    "rules",
                ),
            ).state,
        )
        for requirement in package.evidence_plan.requirements
    )


def _legacy_source_type(requirement: EvidenceRequirement) -> str:
    if requirement.source_requirement == "DOCUMENT":
        return "DOCUMENT"
    if requirement.source_requirement == "ANY":
        return "ANY"
    if requirement.source_requirement == "BOTH":
        return "MIXED"
    return "CODE"


def _coverage_reason(state: str) -> str:
    return {
        "READY": "all CORE evidence requirements are satisfied",
        "PARTIAL": "some CORE evidence requirements remain unresolved",
        "EMPTY": "no direct project evidence reached the final context",
    }[state]


def _coverage_decision_source(
    coverage: tuple[RequirementCoverage, ...],
) -> str:
    if any(item.decision_source == "llm" for item in coverage):
        return "llm"
    if any(item.state == "UNVERIFIED" for item in coverage):
        return "fallback"
    return "rules"
