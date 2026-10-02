from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from devcontext.explanation.models import (
    CLAIM_TYPES,
    CONFIDENCES,
    DEPTHS,
    PRIMARY_STRATEGIES,
    SECTION_TYPES,
    TEACHING_DEVICES,
    ClaimPlan,
    ExplanationPlan,
    ExplanationSection,
)
from devcontext.explanation.prompts import (
    EXPLANATION_PLANNER_SYSTEM_PROMPT,
    allowed_values_block,
)
from devcontext.explanation.policy import (
    DepthDecision,
    LocateSelection,
    SectionBudgetDecision,
    _LOCATE_HINTS,
    decide_depth,
    evidence_requirements_for_labels,
    section_budget_for,
    select_locate_evidence,
)
from devcontext.llm import LLMClient, LLMMessage
from devcontext.observability import llm_stage
from devcontext.models import CONFLICT_RESOLUTIONS, EvidenceConflict
from devcontext.request import EXPLAIN_MODES, UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage


# Baseline V1: successful explanation-planner completion p95=16,144 across
# 27 samples, with zero finish_reason=length.  16,144 * 1.25 rounded up to the
# next 256-token boundary is 20,224.  This remains completion-total budget (not
# visible-only budget), so optional reasoning detail cannot make it unsafe.
EXPLANATION_PLANNER_MAX_TOKENS = 20_224
MAX_SECTIONS = 12
LOCATION_ONLY_MAX_SECTIONS = 2
MAX_STRING_CHARS = 600

_ALLOWED_FIELDS = {
    "answer_goal", "direct_answer", "audience_model", "core_mental_model",
    "primary_strategy", "secondary_strategies", "prerequisite_concepts",
    "likely_misconceptions", "sections", "unresolved_gaps", "conflicts",
    "answer_depth",
}
_SECTION_FIELDS = {
    "id", "title", "section_type", "teaching_goal", "key_points", "claim_plans",
    "evidence_labels", "teaching_devices", "depends_on", "evidence_state",
    "target_tokens",
}
_CLAIM_FIELDS = {
    "claim_goal", "claim_type", "evidence_labels", "confidence", "assumptions",
    "conditional",
}
_SLIM_ALLOWED_FIELDS = {
    "answer_goal", "direct_answer", "core_mental_model", "primary_strategy",
    "sections", "answer_depth",
}
_SLIM_SECTION_FIELDS = {
    "id", "title", "section_type", "teaching_goal", "key_points",
    "evidence_labels", "target_tokens",
}

# Deterministic signals for the fallback only.
_NEGATIVE_HINTS = ("是不是", "是否真的", "有没有", "真的会", "能否")
_FAILURE_HINTS = (
    "失败", "异常", "不一致", "超时", "并发", "重复", "回滚", "补偿", "竞态", "崩溃",
)


class ExplanationPlanError(RuntimeError):
    pass


class ExplanationPlanner:
    """Plans how to explain, after retrieval has been frozen.

    The evidence plan answers "which facts are required"; this answers "in what
    order, and around which mental model, should a person meet them".
    """

    def __init__(
        self,
        llm_client_factory: Callable[[], LLMClient],
        *,
        depth_policy: str = "legacy",
    ) -> None:
        self.llm_client_factory = llm_client_factory
        self.depth_policy = depth_policy
        self.last_client: LLMClient | None = None
        self.last_depth_decision: DepthDecision | None = None
        self.last_section_budget: SectionBudgetDecision | None = None
        self.last_locate_selection = LocateSelection(
            skip_reason="not a deterministic locate request"
        )

    def plan(
        self,
        request: UserRequest,
        evidence_package: EvidencePackage,
    ) -> ExplanationPlan:
        depth = decide_depth(request, self.depth_policy)  # type: ignore[arg-type]
        self.last_depth_decision = depth
        budget = section_budget_for(evidence_package, depth) if depth else None
        self.last_section_budget = budget
        self.last_locate_selection = LocateSelection(
            skip_reason="not a deterministic locate request"
        )
        if budget is not None and budget.target_sections == 0:
            return _no_evidence_plan(request, depth, budget)
        if depth is not None and depth.decision_source == "locate" and budget is not None:
            selection = select_locate_evidence(request, evidence_package, budget)
            self.last_locate_selection = selection
            if selection.used:
                return deterministic_locate_plan(
                    request, evidence_package, depth, budget, selection
                )
        payload = _payload(request, evidence_package)
        if depth is not None and budget is not None:
            payload["planning_constraints"] = {
                "required_answer_depth": depth.answer_depth,
                "preferred_sections": depth.preferred_sections,
                "hard_max_sections": depth.hard_max_sections,
                "required_section_count": budget.target_sections,
                "section_count_reason": (
                    budget.gap_reason
                    or (
                        f"{len(budget.evidence_backed_requirement_ids)} "
                        "evidence-backed requirements are available"
                    )
                ),
            }
        client = self.llm_client_factory()
        self.last_client = client
        with llm_stage("explanation_planning"):
            response = client.generate([
                LLMMessage(
                    "system",
                    EXPLANATION_PLANNER_SYSTEM_PROMPT
                    + "\n\n"
                    + allowed_values_block()
                    + "\n\n"
                    + (
                        "若 planning_constraints 存在，answer_depth 和章节数量"
                        "必须严格使用其中的 required 值；不得用空章节凑数量。"
                        if depth is not None
                        else
                        "回答深度由本阶段决定：若 answer_options.depth_override "
                        "非空，answer_depth 必须使用该值。"
                    ),
                ),
                LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
            ])
        return parse_explanation_plan(
            response,
            request,
            evidence_package,
            depth_decision=depth,
            section_budget=budget,
        )


def parse_explanation_plan(
    response: str,
    request: UserRequest,
    evidence_package: EvidencePackage,
    *,
    depth_decision: DepthDecision | None = None,
    section_budget: SectionBudgetDecision | None = None,
) -> ExplanationPlan:
    try:
        value = json.loads(response)
    except json.JSONDecodeError as exception:
        raise ExplanationPlanError("explanation plan is not valid JSON") from exception
    if isinstance(value, dict) and set(value) == _SLIM_ALLOWED_FIELDS:
        value = _expand_slim_plan(value, evidence_package)
    if not isinstance(value, dict) or set(value) != _ALLOWED_FIELDS:
        raise ExplanationPlanError("explanation plan has invalid fields")

    depth = value["answer_depth"]
    if depth not in DEPTHS:
        raise ExplanationPlanError("answer_depth is invalid")
    if depth_decision is not None and depth != depth_decision.answer_depth:
        raise ExplanationPlanError("answer_depth violates the deterministic policy")
    override = request.answer_options.depth_override
    if override is not None and depth != override:
        raise ExplanationPlanError("answer_depth does not honor the explicit override")
    if depth == "deep" and request.answer_options.answer_mode in EXPLAIN_MODES:
        raise ExplanationPlanError("deep depth is only available on the teach path")

    core_mental_model = _text(value["core_mental_model"], "core_mental_model")
    if not core_mental_model:
        raise ExplanationPlanError("an explanation plan must state a core mental model")

    allowed = _allowed_labels(evidence_package)
    requirement_targets = {
        " ".join(item.target.split()).casefold()
        for item in evidence_package.evidence_plan.requirements
    }
    max_sections = (
        section_budget.hard_max_sections
        if section_budget is not None
        else MAX_SECTIONS
    )
    sections = _parse_sections(
        value["sections"], allowed, requirement_targets, max_sections=max_sections
    )
    if section_budget is not None:
        _validate_section_budget(sections, evidence_package, section_budget)

    strategy = value["primary_strategy"]
    if strategy not in PRIMARY_STRATEGIES:
        raise ExplanationPlanError("primary_strategy is invalid")
    if strategy == "LOCATION_ONLY" and len(sections) > LOCATION_ONLY_MAX_SECTIONS:
        raise ExplanationPlanError("a location-only answer must not be over-planned")
    if strategy == "NEGATIVE_CORRECTION" and not any(
        section.section_type == "MISCONCEPTION" for section in sections
    ):
        raise ExplanationPlanError(
            "an answer correcting a false premise needs a MISCONCEPTION section"
        )

    secondary = _strings(value["secondary_strategies"], "secondary_strategies")
    for item in secondary:
        if item not in PRIMARY_STRATEGIES:
            raise ExplanationPlanError("secondary_strategy is invalid")

    return ExplanationPlan(
        answer_goal=_text(value["answer_goal"], "answer_goal"),
        direct_answer=_text(value["direct_answer"], "direct_answer"),
        audience_model=_text(value["audience_model"], "audience_model"),
        core_mental_model=core_mental_model,
        primary_strategy=strategy,
        sections=sections,
        answer_depth=depth,
        secondary_strategies=secondary,
        prerequisite_concepts=_strings(value["prerequisite_concepts"], "prerequisite_concepts"),
        likely_misconceptions=_strings(value["likely_misconceptions"], "likely_misconceptions"),
        unresolved_gaps=_strings(value["unresolved_gaps"], "unresolved_gaps"),
        conflicts=_parse_conflicts(value["conflicts"], allowed),
    )


def _expand_slim_plan(
    value: dict[str, Any], evidence_package: EvidencePackage
) -> dict[str, Any]:
    """Derive verbose claim/state fields from frozen evidence.

    Legacy full-schema replies remain accepted for compatibility. New planner
    calls only decide the teaching structure; deterministic data already known
    from EvidencePackage is not regenerated by the model.
    """
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list):
        raise ExplanationPlanError("sections must be a list")
    coverage = {
        item.requirement_id: item for item in evidence_package.requirement_coverage
    }
    sections: list[dict[str, Any]] = []
    previous_id: str | None = None
    for raw in raw_sections:
        if not isinstance(raw, dict) or set(raw) != _SLIM_SECTION_FIELDS:
            raise ExplanationPlanError("slim explanation section has invalid fields")
        raw_labels = raw.get("evidence_labels")
        labels = tuple(
            label for label in raw_labels if isinstance(label, str)
        ) if isinstance(raw_labels, list) else ()
        requirement_ids = evidence_requirements_for_labels(
            evidence_package, labels
        )
        states = [
            coverage[requirement_id].state
            for requirement_id in requirement_ids
            if requirement_id in coverage
        ]
        confidence = (
            "CONFIRMED"
            if states and all(state == "SATISFIED" for state in states)
            else "PARTIAL"
            if states
            else "UNVERIFIED"
        )
        claim_type = "PROJECT_FACT" if labels else "GENERAL_CONCEPT"
        section_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
        sections.append({
            **raw,
            "claim_plans": [{
                "claim_goal": raw.get("teaching_goal", ""),
                "claim_type": claim_type,
                "evidence_labels": list(labels),
                "confidence": confidence,
                "assumptions": [],
                "conditional": False,
            }],
            "teaching_devices": ["NONE"],
            "depends_on": [previous_id] if previous_id else [],
            "evidence_state": confidence,
        })
        previous_id = section_id or previous_id
    return {
        **value,
        "audience_model": "熟悉 Java，但需要建立当前项目的实现模型",
        "secondary_strategies": [],
        "prerequisite_concepts": [],
        "likely_misconceptions": [],
        "sections": sections,
        "unresolved_gaps": list(evidence_package.unresolved_requirements),
        "conflicts": [],
    }


def _parse_sections(
    raw_sections: Any,
    allowed: set[str],
    requirement_targets: set[str],
    *,
    max_sections: int = MAX_SECTIONS,
) -> tuple[ExplanationSection, ...]:
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ExplanationPlanError("explanation plan requires at least one section")
    if len(raw_sections) > max_sections:
        raise ExplanationPlanError("explanation plan has too many sections")

    sections: list[ExplanationSection] = []
    ids: set[str] = set()
    titles: set[str] = set()
    for raw in raw_sections:
        if not isinstance(raw, dict) or set(raw) != _SECTION_FIELDS:
            raise ExplanationPlanError("explanation section has invalid fields")
        section_id = _text(raw["id"], "id")
        if not section_id or section_id in ids:
            raise ExplanationPlanError("explanation section ids must be unique")
        ids.add(section_id)

        title = _text(raw["title"], "title")
        normalized_title = " ".join(title.split()).casefold()
        if not normalized_title or normalized_title in titles:
            raise ExplanationPlanError("explanation section titles must not repeat")
        # The investigation order is not the teaching order, so a section named
        # after a requirement is a sign the planner copied instead of planned.
        if normalized_title in requirement_targets:
            raise ExplanationPlanError(
                "explanation sections must not copy evidence requirements"
            )
        titles.add(normalized_title)

        section_type = raw["section_type"]
        if section_type not in SECTION_TYPES:
            raise ExplanationPlanError("section_type is invalid")
        devices = _strings(raw["teaching_devices"], "teaching_devices")
        for device in devices:
            if device not in TEACHING_DEVICES:
                raise ExplanationPlanError("teaching_device is invalid")
        state = raw["evidence_state"]
        if state not in CONFIDENCES:
            raise ExplanationPlanError("evidence_state is invalid")
        target_tokens = raw["target_tokens"]
        if target_tokens is not None and (
            not isinstance(target_tokens, int) or target_tokens < 1
        ):
            raise ExplanationPlanError("target_tokens is invalid")

        sections.append(ExplanationSection(
            id=section_id,
            title=title,
            section_type=section_type,
            teaching_goal=_text(raw["teaching_goal"], "teaching_goal"),
            key_points=_strings(raw["key_points"], "key_points"),
            claim_plans=_parse_claims(raw["claim_plans"], allowed),
            evidence_labels=tuple(_labels(raw["evidence_labels"], allowed)),
            teaching_devices=devices,
            depends_on=_strings(raw["depends_on"], "depends_on"),
            evidence_state=state,
            target_tokens=target_tokens,
        ))
    return tuple(sections)


def _parse_claims(raw_claims: Any, allowed: set[str]) -> tuple[ClaimPlan, ...]:
    if not isinstance(raw_claims, list):
        raise ExplanationPlanError("claim_plans must be a list")
    claims: list[ClaimPlan] = []
    for raw in raw_claims:
        if not isinstance(raw, dict) or set(raw) != _CLAIM_FIELDS:
            raise ExplanationPlanError("claim plan has invalid fields")
        claim_type = raw["claim_type"]
        if claim_type not in CLAIM_TYPES:
            raise ExplanationPlanError("claim_type is invalid")
        confidence = raw["confidence"]
        if confidence not in CONFIDENCES:
            raise ExplanationPlanError("claim confidence is invalid")
        conditional = raw["conditional"]
        if not isinstance(conditional, bool):
            raise ExplanationPlanError("conditional must be a boolean")
        try:
            claims.append(ClaimPlan(
                claim_goal=_text(raw["claim_goal"], "claim_goal"),
                claim_type=claim_type,
                evidence_labels=tuple(_labels(raw["evidence_labels"], allowed)),
                confidence=confidence,
                assumptions=_strings(raw["assumptions"], "assumptions"),
                conditional=conditional,
            ))
        except ValueError as exception:
            raise ExplanationPlanError(str(exception)) from exception
    return tuple(claims)


def _parse_conflicts(raw_conflicts: Any, allowed: set[str]) -> tuple[EvidenceConflict, ...]:
    if not isinstance(raw_conflicts, list):
        raise ExplanationPlanError("conflicts must be a list")
    conflicts: list[EvidenceConflict] = []
    for raw in raw_conflicts:
        if not isinstance(raw, dict) or set(raw) != {
            "topic", "evidence_labels", "resolution", "explanation"
        }:
            raise ExplanationPlanError("conflict has invalid fields")
        if raw["resolution"] not in CONFLICT_RESOLUTIONS:
            raise ExplanationPlanError("conflict resolution is invalid")
        conflicts.append(EvidenceConflict(
            _text(raw["topic"], "topic"),
            tuple(_labels(raw["evidence_labels"], allowed)),
            raw["resolution"],
            _text(raw["explanation"], "explanation"),
        ))
    return tuple(conflicts)


def fallback_explanation_plan(
    request: UserRequest,
    evidence_package: EvidencePackage,
    depth_decision: DepthDecision | None = None,
    section_budget: SectionBudgetDecision | None = None,
) -> ExplanationPlan:
    """A deterministic plan for when the planner call fails.

    Shapes only - it cannot decide what the reader needs to understand, so it
    says so plainly rather than inventing a mental model.
    """
    if depth_decision is not None and section_budget is not None:
        if section_budget.target_sections == 0:
            return _no_evidence_plan(request, depth_decision, section_budget)
        return _budgeted_fallback_plan(
            request, evidence_package, depth_decision, section_budget
        )
    strategy, section_types = _shape_for(request.original_query)
    sections: list[ExplanationSection] = []
    for index, section_type in enumerate(section_types, start=1):
        coverage = _best_coverage(evidence_package)
        labels = _labels_for(evidence_package, coverage.requirement_id if coverage else None)
        sections.append(ExplanationSection(
            id=f"S{index}",
            title=_title_for(section_type),
            section_type=section_type,
            teaching_goal="按证据覆盖情况说明该项目事实",
            key_points=tuple(
                item.target for item in evidence_package.evidence_plan.requirements
            )[:3],
            claim_plans=(
                ClaimPlan(
                    claim_goal="陈述该项目事实",
                    claim_type="PROJECT_FACT" if labels else "GENERAL_CONCEPT",
                    evidence_labels=labels,
                    confidence=_confidence(coverage),
                ),
            ),
            evidence_labels=labels,
            teaching_devices=("NONE",),
            evidence_state=_confidence(coverage),
        ))
    return ExplanationPlan(
        answer_goal="按现有证据说明用户问题的项目事实",
        direct_answer="",
        audience_model="未在本阶段判定（规划器未成功运行）",
        core_mental_model="按证据逐条说明，不额外推断",
        primary_strategy=strategy,
        sections=tuple(sections),
        answer_depth=request.answer_options.depth_override or "standard",
        unresolved_gaps=tuple(
            item.target for item in evidence_package.evidence_plan.requirements
        ),
        decision_source="fallback",
    )


def deterministic_locate_plan(
    request: UserRequest,
    evidence_package: EvidencePackage,
    depth: DepthDecision,
    budget: SectionBudgetDecision,
    selection: LocateSelection,
) -> ExplanationPlan:
    requirement_id = selection.requirement_ids[0]
    requirement = next(
        item
        for item in evidence_package.evidence_plan.requirements
        if item.id == requirement_id
    )
    coverage = _coverage_for(evidence_package, requirement_id)
    labels = selection.evidence_labels
    section = ExplanationSection(
        id="S1",
        title="定位结果",
        section_type="DIRECT_ANSWER",
        teaching_goal="指出实现位置及对应职责",
        key_points=(requirement.target, requirement.success_criteria),
        claim_plans=(
            ClaimPlan(
                claim_goal="根据项目证据指出实现位置",
                claim_type="PROJECT_FACT",
                evidence_labels=labels,
                confidence=_confidence(coverage),
            ),
        ),
        evidence_labels=labels,
        teaching_devices=("NONE",),
        evidence_state=_confidence(coverage),
        target_tokens=400,
    )
    if budget.target_sections != 1:
        raise ExplanationPlanError(
            "a deterministic locate plan requires exactly one section"
        )
    return ExplanationPlan(
        answer_goal="直接指出用户询问的实现位置",
        direct_answer="",
        audience_model="用户需要定位项目中的具体实现",
        core_mental_model="先给位置，再说明该位置承担的职责",
        primary_strategy="LOCATION_ONLY",
        sections=(section,),
        answer_depth=depth.answer_depth,
        decision_source="deterministic",
    )


def _validate_section_budget(
    sections: tuple[ExplanationSection, ...],
    evidence_package: EvidencePackage,
    budget: SectionBudgetDecision,
) -> None:
    if len(sections) > budget.hard_max_sections:
        raise ExplanationPlanError("SECTION_HARD_MAX_EXCEEDED")
    if len(sections) != budget.target_sections:
        raise ExplanationPlanError(
            "SECTION_COUNT_MISMATCH: "
            f"expected {budget.target_sections}, got {len(sections)}"
        )
    supported = set(budget.evidence_backed_requirement_ids)
    teaching_goals: set[str] = set()
    key_point_sets: set[tuple[str, ...]] = set()
    fingerprints: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    covered_requirements: set[str] = set()
    for section in sections:
        teaching_goal = " ".join(section.teaching_goal.split()).casefold()
        if not teaching_goal or teaching_goal in teaching_goals:
            raise ExplanationPlanError("DUPLICATE_SECTION_INTENT")
        teaching_goals.add(teaching_goal)
        normalized_points = tuple(
            sorted(" ".join(point.split()).casefold() for point in section.key_points)
        )
        if (
            not normalized_points
            or any(not point for point in normalized_points)
            or normalized_points in key_point_sets
        ):
            raise ExplanationPlanError("DUPLICATE_SECTION_KEY_POINTS")
        key_point_sets.add(normalized_points)
        if not section.evidence_labels:
            raise ExplanationPlanError("SECTION_WITHOUT_EVIDENCE")
        if any(
            re.fullmatch(r"E\d+", label) is None
            for label in section.evidence_labels
        ):
            raise ExplanationPlanError("SECTION_WITH_INVALID_EVIDENCE_LABEL")
        requirement_ids = (
            evidence_requirements_for_labels(
                evidence_package, section.evidence_labels
            )
            & supported
        )
        if not requirement_ids:
            raise ExplanationPlanError("SECTION_WITHOUT_SUPPORTED_REQUIREMENT")
        for claim in section.claim_plans:
            if claim.claim_type == "PROJECT_FACT" and not set(
                claim.evidence_labels
            ) <= set(section.evidence_labels):
                raise ExplanationPlanError(
                    "PROJECT_FACT_CITATION_OUTSIDE_SECTION"
                )
        fingerprint = (
            section.section_type,
            tuple(sorted(requirement_ids)),
            normalized_points,
        )
        if fingerprint in fingerprints:
            raise ExplanationPlanError("DUPLICATE_SECTION_FINGERPRINT")
        fingerprints.add(fingerprint)
        covered_requirements.update(requirement_ids)
    if len(covered_requirements) < budget.target_sections:
        raise ExplanationPlanError(
            "INSUFFICIENT_DISTINCT_REQUIREMENT_COVERAGE"
        )


def _budgeted_fallback_plan(
    request: UserRequest,
    evidence_package: EvidencePackage,
    depth: DepthDecision,
    budget: SectionBudgetDecision,
) -> ExplanationPlan:
    strategy = _shape_for(request.original_query)[0]
    if strategy == "LOCATION_ONLY" and budget.target_sections > 1:
        strategy = "PROBLEM_SOLUTION"
    default_schedule = (
        "DIRECT_ANSWER",
        "MENTAL_MODEL",
        "MECHANISM",
        "EXECUTION_FLOW",
        "DESIGN_REASON",
        "FAILURE_SCENARIO",
        "SUMMARY",
    )
    if strategy == "NEGATIVE_CORRECTION":
        schedule = (
            "MISCONCEPTION", "DIRECT_ANSWER", "MENTAL_MODEL", "MECHANISM",
            "DESIGN_REASON", "BOUNDARY", "SUMMARY",
        )
    elif strategy == "FAILURE_ANALYSIS":
        schedule = (
            "PROBLEM_SETUP", "EXECUTION_FLOW", "FAILURE_SCENARIO",
            "MECHANISM", "DESIGN_REASON", "BOUNDARY", "SUMMARY",
        )
    else:
        schedule = default_schedule
    requirements = {
        item.id: item for item in evidence_package.evidence_plan.requirements
    }
    sections: list[ExplanationSection] = []
    for index, requirement_id in enumerate(
        budget.evidence_backed_requirement_ids[: budget.target_sections],
        start=1,
    ):
        requirement = requirements[requirement_id]
        labels = _labels_for(evidence_package, requirement_id)
        coverage = _coverage_for(evidence_package, requirement_id)
        section_type = schedule[index - 1]
        sections.append(
            ExplanationSection(
                id=f"S{index}",
                title=_title_for(section_type),
                section_type=section_type,
                teaching_goal=f"解释第 {index} 个有证据支持的项目要点",
                key_points=(
                    requirement.target,
                    requirement.success_criteria,
                    f"证据范围 {requirement.id}",
                ),
                claim_plans=(
                    ClaimPlan(
                        claim_goal="陈述该 Requirement 对应的项目事实",
                        claim_type="PROJECT_FACT",
                        evidence_labels=labels,
                        confidence=_confidence(coverage),
                    ),
                ),
                evidence_labels=labels,
                teaching_devices=("NONE",),
                evidence_state=_confidence(coverage),
            )
        )
    _validate_section_budget(tuple(sections), evidence_package, budget)
    return ExplanationPlan(
        answer_goal="按现有证据说明用户问题的项目事实",
        direct_answer="",
        audience_model="依据检索证据组织回答",
        core_mental_model="每个结论都由对应 Requirement 的项目证据支撑",
        primary_strategy=strategy,
        sections=tuple(sections),
        answer_depth=depth.answer_depth,
        unresolved_gaps=tuple(
            item.target
            for item in evidence_package.evidence_plan.requirements
            if item.id in budget.unsupported_requirement_ids
        ),
        decision_source="fallback",
    )


def _no_evidence_plan(
    request: UserRequest,
    depth: DepthDecision,
    budget: SectionBudgetDecision,
) -> ExplanationPlan:
    return ExplanationPlan(
        answer_goal="说明当前没有可用于回答的项目证据",
        direct_answer="",
        audience_model="用户需要项目事实，但当前检索没有可绑定证据",
        core_mental_model="没有项目证据时不生成项目事实",
        primary_strategy="PROBLEM_SOLUTION",
        sections=(),
        answer_depth=depth.answer_depth,
        unresolved_gaps=(budget.gap_reason or request.original_query,),
        decision_source="no_evidence",
    )


def _coverage_for(
    evidence_package: EvidencePackage, requirement_id: str
):
    return next(
        (
            item
            for item in evidence_package.requirement_coverage
            if item.requirement_id == requirement_id
        ),
        None,
    )


def _shape_for(query: str) -> tuple[str, tuple[str, ...]]:
    if any(hint in query for hint in _NEGATIVE_HINTS):
        return "NEGATIVE_CORRECTION", ("MISCONCEPTION", "DIRECT_ANSWER", "SUMMARY")
    if any(hint in query for hint in _FAILURE_HINTS):
        return "FAILURE_ANALYSIS", (
            "PROBLEM_SETUP", "EXECUTION_FLOW", "FAILURE_SCENARIO", "SUMMARY",
        )
    if any(hint in query for hint in _LOCATE_HINTS):
        return "LOCATION_ONLY", ("DIRECT_ANSWER",)
    return "PROBLEM_SOLUTION", ("PROBLEM_SETUP", "MECHANISM", "SUMMARY")


_TITLES = {
    "DIRECT_ANSWER": "直接回答",
    "PROBLEM_SETUP": "问题是什么",
    "MENTAL_MODEL": "核心模型",
    "MECHANISM": "实现机制",
    "EXECUTION_FLOW": "执行路径",
    "DESIGN_REASON": "为什么这样设计",
    "FAILURE_SCENARIO": "失败时会发生什么",
    "BOUNDARY": "适用边界",
    "MISCONCEPTION": "先纠正一个前提",
    "SUMMARY": "收束",
}


def _title_for(section_type: str) -> str:
    return _TITLES.get(section_type, "说明")


def _confidence(coverage: Any) -> str:
    if coverage is None:
        return "UNVERIFIED"
    return {
        "SATISFIED": "CONFIRMED",
        "PARTIAL": "PARTIAL",
        "MISSING": "UNVERIFIED",
    }.get(coverage.state, "UNVERIFIED")


def _best_coverage(evidence_package: EvidencePackage):
    for coverage in evidence_package.requirement_coverage:
        if coverage.satisfied:
            return coverage
    return evidence_package.requirement_coverage[0] if evidence_package.requirement_coverage else None


def _labels_for(evidence_package: EvidencePackage, requirement_id: str | None) -> tuple[str, ...]:
    if requirement_id is None:
        return ()
    workspace = evidence_package.evidence_workspace
    if workspace is not None:
        return tuple(
            ref.evidence_id
            for ref in workspace.for_requirement(requirement_id)
            if re.fullmatch(r"E\d+", ref.evidence_id)
        )
    by_chunk = {
        item.chunk_id: item.citation.label
        for item in evidence_package.context_bundle.items
    }
    return tuple(
        label
        for label in (
            by_chunk.get(chunk_id)
            for coverage in evidence_package.requirement_coverage
            if coverage.requirement_id == requirement_id
            for chunk_id in coverage.evidence_ids
        )
        if label and re.fullmatch(r"E\d+", label)
    )


def _allowed_labels(evidence_package: EvidencePackage) -> set[str]:
    catalog = evidence_package.evidence_catalog
    if catalog is not None:
        return set(catalog.labels())
    return {item.citation.label for item in evidence_package.context_bundle.items}


def _payload(request: UserRequest, evidence_package: EvidencePackage) -> dict[str, Any]:
    return {
        "question": request.original_query,
        "answer_options": request.answer_options.to_dict(),
        "evidence_plan": evidence_package.evidence_plan.to_dict(),
        "requirement_coverage": [
            {
                "requirement_id": item.requirement_id,
                "state": item.state,
                "missing_criteria": list(item.missing_criteria),
            }
            for item in evidence_package.requirement_coverage
        ],
        "retrieval_state": evidence_package.retrieval_state,
        "available_citations": sorted(_allowed_labels(evidence_package)),
        "evidence": list(_evidence_listing(evidence_package)),
    }


def _evidence_listing(evidence_package: EvidencePackage) -> Sequence[dict[str, Any]]:
    """Everything retrieval found, with bodies, under its workspace labels.

    Sending the retrieval context instead would show the planner C labels while
    the plan is validated against E labels, so every citation it proposed would
    be rejected as unknown.
    """
    workspace = evidence_package.evidence_workspace
    if workspace is not None:
        return tuple(
            {
                "evidence_id": ref.evidence_id,
                "requirement_ids": list(ref.requirement_ids),
                "source_type": ref.citation.source_type,
                "file_path": ref.citation.file_path,
                "class_name": ref.citation.class_name,
                "symbol_name": ref.citation.symbol_name,
                "source_role": ref.source_role,
                "temporal_status": ref.temporal_status,
                # The planner needs enough semantics to organize the teaching
                # path, not complete bodies that the Writer receives later.
                "content_excerpt": (ref.content or "")[:800],
            }
            for ref in workspace.all()
        )
    return tuple(
        {
            "evidence_id": item.citation.label,
            "source_type": item.citation.source_type,
            "file_path": item.citation.file_path,
            "class_name": item.citation.class_name,
            "symbol_name": item.citation.symbol_name,
            "requirement_ids": list(item.sub_question_ids),
            "source_role": item.source_role,
            "temporal_status": item.temporal_status,
            "content_excerpt": item.content[:800],
        }
        for item in evidence_package.context_bundle.items
    )


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ExplanationPlanError(f"{field} must be text")
    text = " ".join(value.strip().split())
    if len(text) > MAX_STRING_CHARS:
        raise ExplanationPlanError(f"{field} is too long")
    return text


def _strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExplanationPlanError(f"{field} must be a list")
    return tuple(_text(item, field) for item in value)


def _labels(value: object, allowed: set[str]) -> list[str]:
    labels = _strings(value, "evidence_labels")
    unknown = [item for item in labels if item not in allowed]
    if unknown:
        raise ExplanationPlanError(f"unknown citation labels: {', '.join(unknown)}")
    return list(labels)
