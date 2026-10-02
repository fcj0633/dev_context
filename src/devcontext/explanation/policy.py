from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from devcontext.request import UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage
    from devcontext.planning.evidence_models import EvidenceRequirement


DepthPolicyName = Literal["legacy", "deterministic"]

_LOCATE_HINTS = (
    "在哪", "哪个类", "哪个文件", "哪里", "谁调用", "调用了", "入口", "位置",
)
_LOCATION_REQUIREMENT_HINTS = (
    *_LOCATE_HINTS, "方法", "实现类", "消费者", "handler",
)
_DETAIL_HINTS = (
    "详细", "深入", "完整", "全面", "逐步", "底层", "源码", "调用链", "原理",
    "设计取舍",
)
_DEPTH_LIMITS = {
    "brief": (1, 2),
    "standard": (3, 4),
    "detailed": (5, 6),
    "deep": (7, 8),
}


@dataclass(frozen=True, slots=True)
class TeachingRuntimeOptions:
    depth_policy: DepthPolicyName = "deterministic"
    section_concurrency: int = 3

    def __post_init__(self) -> None:
        if self.depth_policy not in {"legacy", "deterministic"}:
            raise ValueError("depth_policy must be legacy or deterministic")
        if not 1 <= self.section_concurrency <= 4:
            raise ValueError("section_concurrency must be between 1 and 4")


@dataclass(frozen=True, slots=True)
class DepthDecision:
    answer_depth: str
    decision_source: str
    preferred_sections: int
    hard_max_sections: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SectionBudgetDecision:
    preferred_sections: int
    hard_max_sections: int
    evidence_backed_requirement_ids: tuple[str, ...]
    target_sections: int
    gap_code: str | None = None
    gap_reason: str | None = None
    unsupported_requirement_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["evidence_backed_requirement_ids"] = list(
            self.evidence_backed_requirement_ids
        )
        value["unsupported_requirement_ids"] = list(
            self.unsupported_requirement_ids
        )
        return value


@dataclass(frozen=True, slots=True)
class LocateSelection:
    requirement_ids: tuple[str, ...] = ()
    evidence_labels: tuple[str, ...] = ()
    used: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["requirement_ids"] = list(self.requirement_ids)
        value["evidence_labels"] = list(self.evidence_labels)
        return value


def decide_depth(request: UserRequest, policy: DepthPolicyName) -> DepthDecision | None:
    if policy == "legacy":
        return None
    override = request.answer_options.depth_override
    if override is not None:
        return _depth(override, "explicit")
    query = request.original_query.casefold()
    if any(hint in query for hint in _LOCATE_HINTS):
        return _depth("brief", "locate")
    if any(hint in query for hint in _DETAIL_HINTS):
        return _depth("detailed", "detail_hint")
    return _depth("standard", "default")


def section_budget_for(
    package: EvidencePackage, depth: DepthDecision
) -> SectionBudgetDecision:
    coverage = {
        item.requirement_id: item for item in package.requirement_coverage
    }
    backed: list[str] = []
    unsupported: list[str] = []
    for requirement in package.evidence_plan.requirements:
        status = coverage.get(requirement.id)
        refs = _requirement_labels(package, requirement.id)
        if (
            status is not None
            and status.state in {"SATISFIED", "PARTIAL"}
            and refs
        ):
            backed.append(requirement.id)
        else:
            unsupported.append(requirement.id)
    target = min(
        depth.preferred_sections,
        depth.hard_max_sections,
        len(backed),
    )
    if len(backed) < depth.preferred_sections:
        return SectionBudgetDecision(
            depth.preferred_sections,
            depth.hard_max_sections,
            tuple(backed),
            target,
            "INSUFFICIENT_EVIDENCE_REQUIREMENTS",
            (
                f"只有 {len(backed)} 个 Requirement 有可用证据，"
                f"低于 {depth.answer_depth} 期望的 "
                f"{depth.preferred_sections} 节"
            ),
            tuple(unsupported),
        )
    return SectionBudgetDecision(
        depth.preferred_sections,
        depth.hard_max_sections,
        tuple(backed),
        target,
    )


def select_locate_evidence(
    request: UserRequest,
    package: EvidencePackage,
    budget: SectionBudgetDecision,
) -> LocateSelection:
    backed = set(budget.evidence_backed_requirement_ids)
    requirements = list(package.evidence_plan.requirements)
    coverage = {
        item.requirement_id: item for item in package.requirement_coverage
    }
    direct = [
        requirement
        for requirement in requirements
        if requirement.id in backed and _is_location_requirement(requirement)
    ]
    candidates = direct or [
        requirement
        for requirement in requirements
        if (
            requirement.id in backed
            and requirement.priority == "CORE"
            and requirement.source_requirement in {"CODE", "ANY"}
            and _shares_identifier(request.original_query, requirement)
        )
    ]
    if not candidates:
        return LocateSelection(skip_reason="no related location requirement")
    candidates.sort(
        key=lambda item: (
            item.priority != "CORE",
            item.source_requirement != "CODE",
            getattr(coverage.get(item.id), "state", None) != "SATISFIED",
            requirements.index(item),
        )
    )
    selected = candidates[0]
    labels = _requirement_labels(package, selected.id)
    if not labels:
        return LocateSelection(
            requirement_ids=(selected.id,),
            skip_reason="location requirement has no evidence",
        )
    return LocateSelection((selected.id,), labels, True)


def evidence_requirements_for_labels(
    package: EvidencePackage, labels: tuple[str, ...]
) -> set[str]:
    wanted = set(labels)
    found: set[str] = set()
    workspace = package.evidence_workspace
    if workspace is not None:
        for ref in workspace.by_citation(labels):
            if ref.evidence_id in wanted:
                found.update(ref.requirement_ids)
        return found
    for item in package.context_bundle.items:
        if item.citation.label in wanted:
            found.update(item.sub_question_ids)
    return found


def _depth(answer_depth: str, source: str) -> DepthDecision:
    preferred, hard_max = _DEPTH_LIMITS[answer_depth]
    return DepthDecision(answer_depth, source, preferred, hard_max)


def _requirement_labels(
    package: EvidencePackage, requirement_id: str
) -> tuple[str, ...]:
    def valid(label: str) -> bool:
        return re.fullmatch(r"E\d+", label) is not None

    workspace = package.evidence_workspace
    if workspace is not None:
        return tuple(
            ref.evidence_id
            for ref in workspace.for_requirement(requirement_id)
            if ref.evidence_id and valid(ref.evidence_id)
        )
    return tuple(
        item.citation.label
        for item in package.context_bundle.items
        if (
            requirement_id in item.sub_question_ids
            and item.citation.label
            and valid(item.citation.label)
        )
    )


def _is_location_requirement(requirement: EvidenceRequirement) -> bool:
    text = f"{requirement.target} {requirement.success_criteria}".casefold()
    return any(hint in text for hint in _LOCATION_REQUIREMENT_HINTS)


def _shares_identifier(query: str, requirement: EvidenceRequirement) -> bool:
    requirement_text = f"{requirement.target} {requirement.success_criteria}"
    query_identifiers = set(re.findall(r"[A-Za-z_$][\w$]{2,}", query))
    requirement_identifiers = set(
        re.findall(r"[A-Za-z_$][\w$]{2,}", requirement_text)
    )
    if query_identifiers & requirement_identifiers:
        return True
    query_fragments = {
        query[index:index + 3]
        for index in range(max(0, len(query) - 2))
        if all("\u3400" <= char <= "\u9fff" for char in query[index:index + 3])
    }
    normalized_requirement = " ".join(requirement_text.split())
    return any(fragment in normalized_requirement for fragment in query_fragments)


__all__ = [
    "DepthDecision",
    "LocateSelection",
    "SectionBudgetDecision",
    "TeachingRuntimeOptions",
    "_LOCATE_HINTS",
    "decide_depth",
    "evidence_requirements_for_labels",
    "section_budget_for",
    "select_locate_evidence",
]
