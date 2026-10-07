from __future__ import annotations

import json

from devcontext.llm import LLMMessage
from devcontext.planning import (
    EvidencePlanner,
    QuestionPlan,
    SubQuestion,
    question_plan_to_evidence_plan,
)
from devcontext.planning.evidence_planner import EVIDENCE_PLANNER_SYSTEM_PROMPT


class FakeClient:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[list[LLMMessage]] = []

    def generate(self, messages: list[LLMMessage]) -> str:
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def payload(requirements: list[dict[str, object]] | None = None) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "requirements": requirements or [
                {
                    "target": "确认当前注册入口和调用关系",
                    "success_criteria": "证据指出入口类、方法和下一步调用",
                    "priority": "CORE",
                    "temporal_scope": "CURRENT",
                    "source_requirement": "CODE",
                }
            ],
        },
        ensure_ascii=False,
    )


def test_evidence_planner_produces_requirements_without_queries_or_answer_plan() -> None:
    client = FakeClient(payload())
    plan = EvidencePlanner(lambda: client).plan("当前注册入口在哪里？")

    assert plan.decision_source == "llm"
    assert plan.schema_version == 3
    assert plan.requirements[0].id == "ER1"
    assert plan.requirements[0].source_requirement == "CODE"
    serialized = plan.to_dict()
    assert "answer_depth" not in serialized
    assert "query" not in serialized["requirements"][0]
    assert client.calls[0][0].content == EVIDENCE_PLANNER_SYSTEM_PROMPT
    assert "不生成检索 Query" in EVIDENCE_PLANNER_SYSTEM_PROMPT


def test_evidence_planner_rejects_static_query_and_falls_back_to_any() -> None:
    value = json.loads(payload())
    value["requirements"][0]["query"] = "注册入口"
    plan = EvidencePlanner(
        lambda: FakeClient(json.dumps(value, ensure_ascii=False))
    ).plan("注册入口在哪里？")

    assert plan.decision_source == "fallback"
    assert plan.requirements[0].source_requirement == "ANY"


def test_evidence_planner_requires_core_and_rejects_future_only_current_plan() -> None:
    requirements = [
        {
            "target": "确认未来注册设计",
            "success_criteria": "找到未来规划",
            "priority": "SUPPORTING",
            "temporal_scope": "FUTURE",
            "source_requirement": "DOCUMENT",
        }
    ]
    planner = EvidencePlanner(lambda: FakeClient(payload(requirements)))
    assert planner.plan("当前注册怎么实现？").decision_source == "fallback"


def test_evidence_planner_assigns_ids_and_preserves_requirement_order() -> None:
    first = {
        "target": "确认主流程",
        "success_criteria": "找到入口和状态变化",
        "priority": "CORE",
        "temporal_scope": "CURRENT",
        "source_requirement": "CODE",
    }
    second = {
        "target": "确认设计依据",
        "success_criteria": "找到设计取舍说明",
        "priority": "SUPPORTING",
        "temporal_scope": "CURRENT",
        "source_requirement": "DOCUMENT",
    }
    plan = EvidencePlanner(
        lambda: FakeClient(payload([first, second]))
    ).plan("解释注册实现和设计")

    assert [item.id for item in plan.requirements] == ["ER1", "ER2"]
    assert [item.target for item in plan.requirements] == ["确认主流程", "确认设计依据"]


def test_legacy_question_plan_adapter_is_only_a_data_migration() -> None:
    legacy = QuestionPlan(
        "解释注册实现和设计",
        "理解实现与设计",
        (
            SubQuestion(
                "SQ1",
                "注册如何实现？",
                "解释实现",
                "找到入口和调用关系",
                ("CODE",),
                "静态旧查询",
                "CORE",
                "CURRENT",
            ),
        ),
        "detailed",
        answer_goal="完整解释",
        explanation_strategy="flow",
    )

    evidence = question_plan_to_evidence_plan(legacy)

    assert evidence.requirements[0].target == "注册如何实现？"
    assert evidence.requirements[0].source_requirement == "CODE"
    serialized = evidence.to_dict()
    assert "answer_depth" not in serialized
    assert "retrieval_query" not in serialized["requirements"][0]
