from __future__ import annotations

import json

import pytest

from devcontext.answer import (
    AnswerPlanner,
    fallback_answer_plan,
    fallback_evidence_answer_plan,
)
from devcontext.agentic import EvidencePackage, RequirementCoverage
from devcontext.evidence import EvidenceAnnotation
from devcontext.answer.planner import AnswerPlanError
from devcontext.context import ContextBuilder
from devcontext.models import SearchResult
from devcontext.planning import QuestionPlan, SubQuestion
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


class FakeClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def generate(self, messages) -> str:
        return json.dumps(self.response, ensure_ascii=False)


def search_result(identifier: int = 1) -> SearchResult:
    return SearchResult(
        id=identifier, source_type="CODE", chunk_type="METHOD",
        file_path="Service.java", content="current behavior", start_line=1,
        end_line=2, class_name="Service", symbol_name="run", signature=None,
        title=None, score=1.0,
    )


def question_plan(depth: str = "standard") -> QuestionPlan:
    return QuestionPlan(
        "问题", "理解实现", (
            SubQuestion("SQ1", "如何实现", "建立主线", "实现证据", ("CODE",), "实现 查询", "CORE", "CURRENT"),
        ), depth, answer_goal="理解实现和边界", explanation_strategy="flow",
    )


def valid_response() -> dict[str, object]:
    return {
        "direct_answer": "当前实现通过一条明确链路完成。",
        "summary_citation_labels": ["C1"],
        "explanation_strategy": "flow",
        "sections": [
            {"title": "主流程", "purpose": "解释执行顺序", "key_points": ["入口到写入"], "evidence_labels": ["C1"], "target_chars": 400},
            {"title": "边界", "purpose": "说明保证范围", "key_points": ["当前代码边界"], "evidence_labels": ["C1"], "target_chars": 400},
        ],
        "unresolved_gaps": [],
        "conflicts": [],
    }


def test_answer_planner_binds_sections_to_real_citations() -> None:
    bundle = ContextBuilder().build("问题", [search_result()])
    planner = AnswerPlanner(lambda: FakeClient(valid_response()))

    result = planner.plan("问题", question_plan(), bundle)

    assert result.direct_answer.startswith("当前实现")
    assert result.sections[0].evidence_labels == ("C1",)
    assert result.decision_source == "llm"


def test_answer_planner_rejects_unknown_citation() -> None:
    response = valid_response()
    response["summary_citation_labels"] = ["C99"]
    bundle = ContextBuilder().build("问题", [search_result()])

    with pytest.raises(AnswerPlanError, match="unknown citation"):
        AnswerPlanner(lambda: FakeClient(response)).plan(
            "问题", question_plan(), bundle
        )


def test_fallback_plan_keeps_detailed_shape_without_old_template() -> None:
    bundle = ContextBuilder().build("问题", [search_result()])

    result = fallback_answer_plan(question_plan("detailed"), bundle, ("缺少设计说明",))

    assert result.decision_source == "fallback"
    assert 3 <= len(result.sections) <= 8
    assert all("结论 → 依据" not in section.purpose for section in result.sections)


def test_answer_planner_rejects_direct_copy_of_investigation_items() -> None:
    response = valid_response()
    response["sections"] = [{
        "title": "如何实现",
        "purpose": "照抄调查项",
        "key_points": ["实现"],
        "evidence_labels": ["C1"],
        "target_chars": 300,
    }]
    bundle = ContextBuilder().build("问题", [search_result()])

    with pytest.raises(AnswerPlanError, match="must not copy"):
        AnswerPlanner(lambda: FakeClient(response)).plan(
            "问题", question_plan("brief"), bundle
        )


def evidence_package() -> EvidencePackage:
    plan = EvidencePlan(
        "问题",
        (
            EvidenceRequirement(
                "ER1", "确认当前实现链路", "找到入口与状态写入",
                "CORE", "CURRENT", "CODE",
            ),
        ),
    )
    context = ContextBuilder().build(
        "问题",
        [search_result()],
        {1: EvidenceAnnotation("IMPLEMENTATION", "CURRENT", 100, ("ER1",))},
    )
    coverage = (
        RequirementCoverage("ER1", "SATISFIED", (1,), (), "满足", "llm"),
    )
    return EvidencePackage(
        "问题", plan, context, coverage, (), "READY", (), (),
    )


def test_evidence_answer_planner_owns_depth_goal_and_structure() -> None:
    response = valid_response()
    response["answer_goal"] = "让读者理解当前实现和边界"
    response["answer_depth"] = "standard"
    request = UserRequest("问题", AnswerOptions("standard", "explain"))

    result = AnswerPlanner(lambda: FakeClient(response)).plan_evidence(
        request, evidence_package()
    )

    assert result.answer_goal == "让读者理解当前实现和边界"
    assert result.answer_depth == "standard"
    assert result.sections[0].title == "主流程"
    assert "answer_depth" not in evidence_package().evidence_plan.to_dict()


def test_evidence_answer_planner_must_honor_explicit_depth_override() -> None:
    response = valid_response()
    response["answer_goal"] = "解释当前实现"
    response["answer_depth"] = "brief"

    with pytest.raises(AnswerPlanError, match="override"):
        AnswerPlanner(lambda: FakeClient(response)).plan_evidence(
            UserRequest("问题", AnswerOptions("standard", "explain")),
            evidence_package(),
        )


def test_evidence_fallback_plan_uses_answer_options_not_evidence_plan() -> None:
    result = fallback_evidence_answer_plan(
        UserRequest("问题", AnswerOptions("detailed", "explain")),
        evidence_package(),
    )

    assert result.answer_depth == "detailed"
    assert result.decision_source == "fallback"
    assert 3 <= len(result.sections) <= 8
