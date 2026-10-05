from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from devcontext.explanation.models import (
    CLAIM_TYPES,
    CONFIDENCES,
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
from devcontext.llm import LLMClient, LLMMessage
from devcontext.observability import llm_stage
from devcontext.models import CONFLICT_RESOLUTIONS, EvidenceConflict
from devcontext.request import EXPLAIN_MODES, UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage


EXPLANATION_PLANNER_MAX_TOKENS = 32_768
MAX_SECTIONS = 12
LOCATION_ONLY_MAX_SECTIONS = 2
MAX_STRING_CHARS = 600

_ALLOWED_FIELDS = {
    "answer_goal", "direct_answer", "audience_model", "core_mental_model",
    "primary_strategy", "secondary_strategies", "prerequisite_concepts",
    "likely_misconceptions", "sections", "unresolved_gaps", "conflicts",
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

# Deterministic signals for the fallback only. The LLM path decides all of this
# itself; these exist so a planner failure still yields a usable shape.
_LOCATE_HINTS = ("在哪", "哪个类", "哪个文件", "哪里", "谁调用", "调用了", "入口", "位置")
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

    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory
        self.last_client: LLMClient | None = None

    def plan(
        self,
        request: UserRequest,
        evidence_package: EvidencePackage,
    ) -> ExplanationPlan:
        payload = _payload(request, evidence_package)
        self.last_client = self.llm_client_factory()
        with llm_stage("explanation_planning"):
            response = self.last_client.generate([
                LLMMessage(
                    "system",
                    EXPLANATION_PLANNER_SYSTEM_PROMPT
                    + "\n\n"
                    + allowed_values_block()
                    + "\n\n"
                    "按用户理解任务和背景决定展开，不输出深度档位或为了长度凑章节。",
                ),
                LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
            ])
        return parse_explanation_plan(response, request, evidence_package)


def parse_explanation_plan(
    response: str,
    request: UserRequest,
    evidence_package: EvidencePackage,
) -> ExplanationPlan:
    try:
        value = json.loads(response)
    except json.JSONDecodeError as exception:
        raise ExplanationPlanError("explanation plan is not valid JSON") from exception
    if isinstance(value, dict):
        value.pop("answer_depth", None)
    if not isinstance(value, dict) or set(value) != _ALLOWED_FIELDS:
        raise ExplanationPlanError("explanation plan has invalid fields")

    core_mental_model = _text(value["core_mental_model"], "core_mental_model")
    if not core_mental_model:
        raise ExplanationPlanError("an explanation plan must state a core mental model")

    allowed = _allowed_labels(evidence_package)
    requirement_targets = {
        " ".join(item.target.split()).casefold()
        for item in evidence_package.evidence_plan.requirements
    }
    sections = _parse_sections(value["sections"], allowed, requirement_targets)

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
        secondary_strategies=secondary,
        prerequisite_concepts=_strings(value["prerequisite_concepts"], "prerequisite_concepts"),
        likely_misconceptions=_strings(value["likely_misconceptions"], "likely_misconceptions"),
        unresolved_gaps=_strings(value["unresolved_gaps"], "unresolved_gaps"),
        conflicts=_parse_conflicts(value["conflicts"], allowed),
    )


def _parse_sections(
    raw_sections: Any,
    allowed: set[str],
    requirement_targets: set[str],
) -> tuple[ExplanationSection, ...]:
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ExplanationPlanError("explanation plan requires at least one section")
    if len(raw_sections) > MAX_SECTIONS:
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
) -> ExplanationPlan:
    """A deterministic plan for when the planner call fails.

    Shapes only - it cannot decide what the reader needs to understand, so it
    says so plainly rather than inventing a mental model.
    """
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
        unresolved_gaps=tuple(
            item.target for item in evidence_package.evidence_plan.requirements
        ),
        decision_source="fallback",
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
    "FAILURE_SCENARIO": "失败时会发生什么",
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
            ref.evidence_id for ref in workspace.for_requirement(requirement_id)
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
        if label
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
            item.to_dict() for item in evidence_package.requirement_coverage
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
        return tuple(ref.to_dict(include_content=True) for ref in workspace.all())
    return tuple(
        {
            "evidence_id": item.citation.label,
            "citation": item.citation.to_dict(),
            "requirement_ids": list(item.sub_question_ids),
            "source_role": item.source_role,
            "temporal_status": item.temporal_status,
            "content": item.content,
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
