from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from devcontext.answer.models import REVIEW_ISSUE_TYPES, TEACHING_ISSUE_TYPES
from devcontext.explanation.grounding import GroundingIssue
from devcontext.explanation.models import DraftSection, ExplanationPlan
from devcontext.llm import LLMClient, LLMMessage
from devcontext.observability import llm_stage, mark_last_call_wasted


# Both vocabularies: the eight that predate this path are still meaningful,
# and a teaching review that could not say UNSUPPORTED_CLAIM would be worse
# at its job than the reviewer it replaces.
ALL_ISSUE_TYPES = REVIEW_ISSUE_TYPES + TEACHING_ISSUE_TYPES

REVIEWER_MAX_TOKENS = 32_768
MAX_DESCRIPTION_CHARS = 400
# One round. A second pass would be reviewing a review, and the measured effect
# of a third is noise while the cost is a full regeneration.
MAX_REVISION_ROUNDS = 1

REVIEW_SYSTEM_PROMPT = """你是 DevContext-Java 的教学型解释审稿器。检查这份回答是否既正确、又真的把问题讲清楚了。

事实与证据：
- 项目事实必须有 Citation 支撑；把通用技术知识写成"本项目就是这样实现的"属于严重问题。
- 把基于证据的推导与证据本身混为一谈（写成像是直接证据）属于 FACT_INFERENCE_CONFUSION。
- 章节引用了不属于它的证据，或结论超出该章节绑定的证据范围，属于 SECTION_EVIDENCE_MISMATCH。

教学效果：
- 全文有没有一条贯穿的核心心智模型？只在某节提一次不算，缺了报 MISSING_MENTAL_MODEL。
- 关键设计有没有解释"为什么"？只有"是什么"、缺动机与取舍，报 MISSING_WHY。
- 展开顺序是否由浅入深、有铺垫？跳跃或倒置报 POOR_SCAFFOLDING；章节之间缺少过渡可报 ABRUPT_TRANSITION。
- 假设案例是否明确写成"假设/例如"？写成项目真实行为的报 MISLABELED_EXAMPLE。
- 是否有与理解无关的堆砌细节？报 UNHELPFUL_DETAIL。

issue_type 只能取这些值之一：""" + "、".join(ALL_ISSUE_TYPES) + """。
只报告真正的问题；没有问题就返回空的 issues。最多修订一次。
只输出严格 JSON：
{"accepted": true/false,
 "global_issues": [{"issue_type": "...", "description": "..."}],
 "section_issues": [{"section_id": "S1", "issue_type": "...", "description": "..."}],
 "revision_required": ["S1"]}"""


@dataclass(frozen=True, slots=True)
class SectionIssue:
    section_id: str
    issue_type: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {
            "section_id": self.section_id,
            "issue_type": self.issue_type,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class TeachingReviewResult:
    accepted: bool
    global_issues: tuple[dict[str, str], ...] = ()
    section_issues: tuple[SectionIssue, ...] = ()
    revision_required: tuple[str, ...] = ()
    decision_source: str = "llm"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "global_issues": list(self.global_issues),
            "section_issues": [item.to_dict() for item in self.section_issues],
            "revision_required": list(self.revision_required),
            "decision_source": self.decision_source,
            "error": self.error,
        }


class TeachingReviewer:
    """Reviews the whole argument, and says which sections to redo.

    It does not rewrite: naming the bad sections and regenerating only those is
    both cheaper and safer than letting a reviewer replace an answer that was
    mostly fine.
    """

    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory
        self.last_client: LLMClient | None = None

    def review(
        self,
        query: str,
        plan: ExplanationPlan,
        drafts: Sequence[DraftSection],
    ) -> TeachingReviewResult:
        known = {draft.section_id for draft in drafts}
        payload = {
            "question": query,
            "core_mental_model": plan.core_mental_model,
            "answer_goal": plan.answer_goal,
            "primary_strategy": plan.primary_strategy,
            "answer_depth": plan.answer_depth,
            "unresolved_gaps": list(plan.unresolved_gaps),
            "sections": [
                {
                    "id": draft.section_id,
                    "title": draft.title,
                    "evidence_state": draft.evidence_state,
                    "used_citations": list(draft.used_citations),
                    "text": draft.text_with_citations,
                }
                for draft in drafts
            ],
        }
        self.last_client = self.llm_client_factory()
        try:
            with llm_stage("teaching_review"):
                response = self.last_client.generate([
                    LLMMessage("system", REVIEW_SYSTEM_PROMPT),
                    LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
                ])
            return _parse_review(response, known)
        except Exception as exception:
            from devcontext.agentic.models import error_detail

            # A review that could not be parsed still cost a full call, and a
            # review is the most expensive single stage after the writer.
            mark_last_call_wasted(
                "review reply unusable; accepted by default", stage="teaching_review"
            )
            return TeachingReviewResult(
                accepted=True,
                decision_source="fallback",
                error=error_detail(exception),
            )


def _parse_review(response: str, known_ids: set[str]) -> TeachingReviewResult:
    value = json.loads(response)
    if not isinstance(value, dict) or set(value) != {
        "accepted", "global_issues", "section_issues", "revision_required"
    }:
        raise ValueError("teaching review returned invalid fields")
    accepted = value["accepted"]
    if not isinstance(accepted, bool):
        raise ValueError("teaching review accepted must be a boolean")

    global_issues = tuple(
        _issue(item) for item in _list(value["global_issues"], "global_issues")
    )
    section_issues = tuple(
        _section_issue(item, known_ids)
        for item in _list(value["section_issues"], "section_issues")
    )
    # A review may only ask to redo sections that exist; anything else is the
    # reviewer inventing work, and acting on it would waste a generation.
    revision_required = tuple(
        section_id
        for section_id in _strings(value["revision_required"], "revision_required")
        if section_id in known_ids
    )
    return TeachingReviewResult(
        accepted=accepted,
        global_issues=global_issues,
        section_issues=section_issues,
        revision_required=revision_required,
    )


def _issue(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != {"issue_type", "description"}:
        raise ValueError("teaching review issue has invalid fields")
    issue_type = raw["issue_type"]
    if issue_type not in ALL_ISSUE_TYPES:
        raise ValueError("teaching review issue_type is invalid")
    return {"issue_type": issue_type, "description": _text(raw["description"])}


def _section_issue(raw: Any, known_ids: set[str]) -> SectionIssue:
    if not isinstance(raw, dict) or set(raw) != {
        "section_id", "issue_type", "description"
    }:
        raise ValueError("teaching section issue has invalid fields")
    section_id = raw["section_id"]
    if section_id not in known_ids:
        raise ValueError("teaching section issue names an unknown section")
    issue_type = raw["issue_type"]
    if issue_type not in ALL_ISSUE_TYPES:
        raise ValueError("teaching section issue_type is invalid")
    return SectionIssue(section_id, issue_type, _text(raw["description"]))


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("teaching review text must not be empty")
    return " ".join(value.split())[:MAX_DESCRIPTION_CHARS]


def _list(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"teaching review {field_name} must be a list")
    return value


def _strings(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ValueError(f"teaching review {field_name} must be a string list")
    return value


def needs_llm_review(
    plan: ExplanationPlan,
    deterministic_issues: Sequence[GroundingIssue],
) -> bool:
    """Three tiers, so a one-line answer does not pay for a full review.

    ``brief`` and location-only answers are checked by the deterministic rules
    alone: they are one or two sentences, and a reviewer that can rewrite them
    has more to lose than to add.
    """
    if plan.answer_depth in {"detailed", "deep"}:
        return True
    if plan.primary_strategy == "LOCATION_ONLY" or plan.answer_depth == "brief":
        return False
    return bool(deterministic_issues) or bool(plan.conflicts)
