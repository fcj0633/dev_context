from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from devcontext.answer.models import (
    CONFLICT_RESOLUTIONS,
    AnswerPlan,
    AnswerSection,
)
from devcontext.models import EvidenceConflict
from devcontext.llm import LLMClient, LLMMessage
from devcontext.models import ContextBundle
from devcontext.planning import QuestionPlan
from devcontext.request import UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage


DEPTH_LIMITS = {
    "brief": (0, 2, 150, 500),
    "standard": (2, 5, 800, 1800),
    "detailed": (3, 8, 2200, 5000),
}

ANSWER_PLANNER_SYSTEM_PROMPT = """你是 DevContext-Java 的回答编排器。你只设计回答结构，不写最终长文。
依据 Question Plan 与带 Citation 的 Context，先形成一个有证据支持的直接答案，再把调查项合并为读者容易理解的章节。
不得把调查项一对一复制成章节；不得把历史计划写成当前实现；当前实现问题优先依据 IMPLEMENTATION/CURRENT 代码证据，设计文档只证明设计意图。
VERIFICATION 只能证明报告明确记录的测试结果；HISTORICAL 或 FUTURE 证据不能覆盖当前代码。冲突无法消解时必须选择 UNRESOLVED 并解释。
每节只能绑定 Context 中真实存在的 Citation。证据不足集中写入 unresolved_gaps。
冲突 resolution 只能是 CURRENT_IMPLEMENTATION_WINS、CURRENT_VERIFICATION_WINS、DESIGN_INTENT_ONLY 或 UNRESOLVED。
只输出严格 JSON，不要 Markdown。"""


class AnswerPlanError(RuntimeError):
    pass


class AnswerPlanner:
    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory
        self.last_client: LLMClient | None = None

    def plan(
        self,
        query: str,
        question_plan: QuestionPlan,
        context: ContextBundle,
        unresolved_gaps: Sequence[str] = (),
    ) -> AnswerPlan:
        prompt = {
            "question": query,
            "question_plan": question_plan.to_dict(),
            "available_citations": [item.citation.label for item in context.items],
            "known_gaps": list(unresolved_gaps),
            "context": context.rendered_text,
            "section_constraints": {
                "minimum_sections": DEPTH_LIMITS[question_plan.answer_depth][0],
                "maximum_sections": DEPTH_LIMITS[question_plan.answer_depth][1],
                "minimum_total_target_chars": DEPTH_LIMITS[question_plan.answer_depth][2],
                "maximum_total_target_chars": DEPTH_LIMITS[question_plan.answer_depth][3],
            },
            "output_schema": {
                "direct_answer": "string",
                "summary_citation_labels": ["C1"],
                "explanation_strategy": "string",
                "sections": [{
                    "title": "string", "purpose": "string",
                    "key_points": ["string"], "evidence_labels": ["C1"],
                    "target_chars": 400,
                }],
                "unresolved_gaps": ["string"],
                "conflicts": [{
                    "topic": "string", "evidence_labels": ["C1", "C2"],
                    "resolution": "UNRESOLVED", "explanation": "string",
                }],
            },
        }
        self.last_client = self.llm_client_factory()
        response = self.last_client.generate([
            LLMMessage("system", ANSWER_PLANNER_SYSTEM_PROMPT),
            LLMMessage("user", json.dumps(prompt, ensure_ascii=False)),
        ])
        return self._parse(response, question_plan, context)

    def plan_evidence(
        self,
        request: UserRequest,
        evidence_package: EvidencePackage,
    ) -> AnswerPlan:
        """Plan presentation only after evidence retrieval has been frozen."""
        context = evidence_package.context_bundle
        payload = {
            "question": request.original_query,
            "answer_options": request.answer_options.to_dict(),
            "evidence_plan": evidence_package.evidence_plan.to_dict(),
            "requirement_coverage": [
                item.to_dict() for item in evidence_package.requirement_coverage
            ],
            "retrieval_state": evidence_package.retrieval_state,
            "available_citations": [item.citation.label for item in context.items],
            "context": context.rendered_text,
            "depth_constraints": {
                depth: {
                    "minimum_sections": limits[0],
                    "maximum_sections": limits[1],
                    "minimum_total_target_chars": limits[2],
                    "maximum_total_target_chars": limits[3],
                }
                for depth, limits in DEPTH_LIMITS.items()
            },
            "output_schema": {
                "answer_goal": "string",
                "answer_depth": "brief|standard|detailed",
                "direct_answer": "string",
                "summary_citation_labels": ["C1"],
                "explanation_strategy": "string",
                "sections": [{
                    "title": "string", "purpose": "string",
                    "key_points": ["string"], "evidence_labels": ["C1"],
                    "target_chars": 400,
                }],
                "unresolved_gaps": ["string"],
                "conflicts": [{
                    "topic": "string", "evidence_labels": ["C1", "C2"],
                    "resolution": "UNRESOLVED", "explanation": "string",
                }],
            },
        }
        system_prompt = ANSWER_PLANNER_SYSTEM_PROMPT + """
回答深度、回答目标、解释策略和章节结构全部由本阶段决定；Evidence Requirement 只是事实覆盖契约，不是章节目录。
不得按 Requirement 一一生成章节。若 answer_options.depth_override 非空，answer_depth 必须使用该值；否则根据用户问题决定。
输出 JSON 还必须包含 answer_goal 和 answer_depth。"""
        self.last_client = self.llm_client_factory()
        response = self.last_client.generate([
            LLMMessage("system", system_prompt),
            LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
        ])
        return self._parse_evidence(response, request, evidence_package)

    @staticmethod
    def _parse(
        response: str, question_plan: QuestionPlan, context: ContextBundle
    ) -> AnswerPlan:
        try:
            value = json.loads(response)
        except json.JSONDecodeError as exception:
            raise AnswerPlanError("answer plan is not valid JSON") from exception
        required = {
            "direct_answer", "summary_citation_labels", "explanation_strategy",
            "sections", "unresolved_gaps", "conflicts",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise AnswerPlanError("answer plan has invalid fields")
        allowed = {item.citation.label for item in context.items}
        summary = _labels(value["summary_citation_labels"], allowed)
        if not summary:
            raise AnswerPlanError("direct answer requires a summary citation")
        raw_sections = value["sections"]
        minimum, maximum, min_chars, max_chars = DEPTH_LIMITS[
            question_plan.answer_depth
        ]
        if not isinstance(raw_sections, list) or not minimum <= len(raw_sections) <= maximum:
            raise AnswerPlanError("answer plan section count is invalid")
        sections: list[AnswerSection] = []
        titles: set[str] = set()
        for raw in raw_sections:
            if not isinstance(raw, dict) or set(raw) != {
                "title", "purpose", "key_points", "evidence_labels", "target_chars"
            }:
                raise AnswerPlanError("answer section has invalid fields")
            title = _text(raw["title"], "title")
            if title.casefold() in titles:
                raise AnswerPlanError("answer section titles must not repeat")
            titles.add(title.casefold())
            points = _strings(raw["key_points"], "key_points")
            labels = _labels(raw["evidence_labels"], allowed)
            target = raw["target_chars"]
            if not isinstance(target, int) or target < 1:
                raise AnswerPlanError("target_chars is invalid")
            if points and not labels:
                raise AnswerPlanError("factual answer section requires evidence")
            sections.append(AnswerSection(
                title, _text(raw["purpose"], "purpose"), points, labels, target
            ))
        planned_questions = {
            " ".join(item.question.split()).casefold()
            for item in question_plan.sub_questions
        }
        if sections and all(
            " ".join(section.title.split()).casefold() in planned_questions
            for section in sections
        ):
            raise AnswerPlanError("answer sections must not copy investigation items")
        total = sum(item.target_chars for item in sections)
        if sections and not min_chars <= total <= max_chars:
            raise AnswerPlanError("answer plan target length is invalid")
        conflicts: list[EvidenceConflict] = []
        if not isinstance(value["conflicts"], list):
            raise AnswerPlanError("conflicts must be a list")
        for raw in value["conflicts"]:
            if not isinstance(raw, dict) or set(raw) != {
                "topic", "evidence_labels", "resolution", "explanation"
            }:
                raise AnswerPlanError("conflict has invalid fields")
            if raw["resolution"] not in CONFLICT_RESOLUTIONS:
                raise AnswerPlanError("conflict resolution is invalid")
            conflicts.append(EvidenceConflict(
                _text(raw["topic"], "topic"),
                _labels(raw["evidence_labels"], allowed),
                raw["resolution"],
                _text(raw["explanation"], "explanation"),
            ))
        return AnswerPlan(
            _text(value["direct_answer"], "direct_answer"),
            summary,
            _text(value["explanation_strategy"], "explanation_strategy"),
            tuple(sections),
            _strings(value["unresolved_gaps"], "unresolved_gaps", allow_empty=True),
            tuple(conflicts),
        )

    @staticmethod
    def _parse_evidence(
        response: str,
        request: UserRequest,
        evidence_package: EvidencePackage,
    ) -> AnswerPlan:
        try:
            value = json.loads(response)
        except json.JSONDecodeError as exception:
            raise AnswerPlanError("answer plan is not valid JSON") from exception
        required = {
            "answer_goal", "answer_depth", "direct_answer",
            "summary_citation_labels", "explanation_strategy", "sections",
            "unresolved_gaps", "conflicts",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise AnswerPlanError("answer plan has invalid fields")
        depth = value["answer_depth"]
        if depth not in DEPTH_LIMITS:
            raise AnswerPlanError("answer_depth is invalid")
        override = request.answer_options.depth_override
        if override is not None and depth != override:
            raise AnswerPlanError("answer_depth does not honor the explicit override")

        context = evidence_package.context_bundle
        allowed = {item.citation.label for item in context.items}
        summary = _labels(value["summary_citation_labels"], allowed)
        if not summary:
            raise AnswerPlanError("direct answer requires a summary citation")
        minimum, maximum, min_chars, max_chars = DEPTH_LIMITS[depth]
        raw_sections = value["sections"]
        if not isinstance(raw_sections, list) or not minimum <= len(raw_sections) <= maximum:
            raise AnswerPlanError("answer plan section count is invalid")
        requirement_targets = {
            " ".join(item.target.split()).casefold()
            for item in evidence_package.evidence_plan.requirements
        }
        sections: list[AnswerSection] = []
        titles: set[str] = set()
        for raw in raw_sections:
            if not isinstance(raw, dict) or set(raw) != {
                "title", "purpose", "key_points", "evidence_labels", "target_chars"
            }:
                raise AnswerPlanError("answer section has invalid fields")
            title = _text(raw["title"], "title")
            normalized_title = " ".join(title.split()).casefold()
            if normalized_title in titles:
                raise AnswerPlanError("answer section titles must not repeat")
            if normalized_title in requirement_targets:
                raise AnswerPlanError("answer sections must not copy evidence requirements")
            titles.add(normalized_title)
            points = _strings(raw["key_points"], "key_points")
            labels = _labels(raw["evidence_labels"], allowed)
            target = raw["target_chars"]
            if not isinstance(target, int) or target < 1:
                raise AnswerPlanError("target_chars is invalid")
            if points and not labels:
                raise AnswerPlanError("factual answer section requires evidence")
            sections.append(AnswerSection(
                title, _text(raw["purpose"], "purpose"), points, labels, target
            ))
        total = sum(item.target_chars for item in sections)
        if sections and not min_chars <= total <= max_chars:
            raise AnswerPlanError("answer plan target length is invalid")
        conflicts = _parse_conflicts(value["conflicts"], allowed)
        return AnswerPlan(
            _text(value["direct_answer"], "direct_answer"),
            summary,
            _text(value["explanation_strategy"], "explanation_strategy"),
            tuple(sections),
            _strings(value["unresolved_gaps"], "unresolved_gaps", allow_empty=True),
            conflicts,
            "llm",
            _text(value["answer_goal"], "answer_goal"),
            depth,
        )


def fallback_answer_plan(
    question_plan: QuestionPlan, context: ContextBundle, gaps: Sequence[str]
) -> AnswerPlan:
    labels = tuple(item.citation.label for item in context.items)
    minimum, maximum, min_chars, max_chars = DEPTH_LIMITS[question_plan.answer_depth]
    selected = list(question_plan.sub_questions[:maximum])
    section_count = max(1, max(minimum, len(selected)))
    per_section = max(150, (min_chars + section_count - 1) // section_count)
    sections_list = [
        AnswerSection(
            question.question,
            question.purpose,
            (question.evidence_description,),
            labels,
            per_section,
        )
        for question in selected
    ]
    conservative_sections = (
        ("直接回答", "先回应用户真正的问题", "只陈述现有证据能够支持的中心判断"),
        ("证据与实现关系", "解释证据怎样共同支撑判断", "区分实现事实、验证结果和设计意图"),
        ("边界与不能确认的部分", "集中交代证据边界", "说明冲突、缺口和当前不能确认的内容"),
    )
    for title, purpose, point in conservative_sections:
        if len(sections_list) >= minimum:
            break
        sections_list.append(
            AnswerSection(title, purpose, (point,), labels, per_section)
        )
    sections = tuple(sections_list[:maximum])
    return AnswerPlan(
        question_plan.answer_goal or question_plan.intent_summary,
        labels[:2],
        question_plan.explanation_strategy,
        sections,
        tuple(gaps),
        (),
        "fallback",
        question_plan.answer_goal or question_plan.intent_summary,
        question_plan.answer_depth,
    )


def fallback_evidence_answer_plan(
    request: UserRequest,
    evidence_package: EvidencePackage,
) -> AnswerPlan:
    context = evidence_package.context_bundle
    labels = tuple(item.citation.label for item in context.items)
    depth = request.answer_options.depth_override or _default_depth(
        len(evidence_package.evidence_plan.requirements)
    )
    minimum, maximum, min_chars, _ = DEPTH_LIMITS[depth]
    requirements = list(evidence_package.evidence_plan.requirements)
    section_count = max(1, min(maximum, max(minimum, min(3, len(requirements)))))
    per_section = max(150, (min_chars + section_count - 1) // section_count)
    generic = (
        ("直接回答", "先回应用户真正的问题", "只陈述现有证据直接支持的中心判断"),
        ("实现与依据", "组织当前项目证据", "按读者理解顺序解释实现、边界和关系"),
        ("证据边界", "集中说明未解决内容", "区分已确认事实与当前不能确认的部分"),
    )
    sections = tuple(
        AnswerSection(title, purpose, (point,), labels, per_section)
        for title, purpose, point in generic[:section_count]
    )
    coverage_by_id = {
        item.requirement_id: item for item in evidence_package.requirement_coverage
    }
    gaps = tuple(
        f"{item.target}：{coverage_by_id[item.id].reason}"
        for item in requirements
        if item.id in coverage_by_id and not coverage_by_id[item.id].satisfied
    )
    return AnswerPlan(
        "当前回答只能陈述已进入证据包的项目事实，未满足的核心证据需要明确保留为未知。",
        labels[:2],
        "mixed",
        sections,
        gaps,
        (),
        "fallback",
        f"依据当前项目证据回答：{request.original_query}",
        depth,
    )


def _parse_conflicts(
    value: object,
    allowed: set[str],
) -> tuple[EvidenceConflict, ...]:
    if not isinstance(value, list):
        raise AnswerPlanError("conflicts must be a list")
    conflicts: list[EvidenceConflict] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {
            "topic", "evidence_labels", "resolution", "explanation"
        }:
            raise AnswerPlanError("conflict has invalid fields")
        if raw["resolution"] not in CONFLICT_RESOLUTIONS:
            raise AnswerPlanError("conflict resolution is invalid")
        conflicts.append(EvidenceConflict(
            _text(raw["topic"], "topic"),
            _labels(raw["evidence_labels"], allowed),
            raw["resolution"],
            _text(raw["explanation"], "explanation"),
        ))
    return tuple(conflicts)


def _default_depth(requirement_count: int) -> str:
    if requirement_count <= 1:
        return "brief"
    if requirement_count <= 3:
        return "standard"
    return "detailed"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnswerPlanError(f"{name} must be non-empty text")
    return " ".join(value.split())


def _strings(value: object, name: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise AnswerPlanError(f"{name} must be a list")
    return tuple(_text(item, name) for item in value)


def _labels(value: object, allowed: set[str]) -> tuple[str, ...]:
    labels = _strings(value, "evidence_labels", allow_empty=True)
    if any(label not in allowed for label in labels):
        raise AnswerPlanError("answer plan contains an unknown citation")
    return labels
