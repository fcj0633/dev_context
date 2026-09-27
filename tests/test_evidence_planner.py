from __future__ import annotations

import json

import pytest

from devcontext.llm import LLMMessage
from devcontext.planning import (
    EvidencePlanner,
    QuestionPlan,
    SubQuestion,
)
from devcontext.planning.evidence_planner import EVIDENCE_PLANNER_SYSTEM_PROMPT

QUESTION_PLAN = QuestionPlan(
    original_query="订单关闭为何这样设计",
    intent_summary="了解订单关闭的实现与设计依据",
    sub_questions=(
        SubQuestion("SQ1", "订单关闭的入口方法在哪里", "定位实现"),
        SubQuestion("SQ2", "订单关闭的设计依据是什么", "确认设计原因"),
    ),
    answer_depth="detailed",
)


class FakeLLMClient:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[list[LLMMessage]] = []

    def generate(self, messages: list[LLMMessage]) -> str:
        self.calls.append(list(messages))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def planner(response: str | Exception) -> tuple[EvidencePlanner, FakeLLMClient]:
    client = FakeLLMClient(response)
    return EvidencePlanner(lambda: client), client


def valid_plan(**overrides: object) -> str:
    payload = {
        "requirements": [
            {"description": "订单关闭的触发与入口实现", "preferred_sources": ["CODE"]},
            {"description": "订单关闭时序与取舍的说明", "preferred_sources": ["DOCUMENT"]},
        ]
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_valid_plan_is_accepted() -> None:
    subject, client = planner(valid_plan())

    plan = subject.plan(QUESTION_PLAN)

    assert plan.decision_source == "llm"
    assert [item.id for item in plan.requirements] == ["ER1", "ER2"]
    assert [item.sub_question_id for item in plan.requirements] == ["SQ1", "SQ2"]
    assert plan.requirements[0].preferred_sources == ("CODE",)
    assert plan.requirements[1].preferred_sources == ("DOCUMENT",)
    assert client.calls[0][0].content == EVIDENCE_PLANNER_SYSTEM_PROMPT
    assert "SQ1. 订单关闭的入口方法在哪里" in client.calls[0][1].content


def test_two_sources_are_allowed() -> None:
    subject, _ = planner(
        valid_plan(
            requirements=[
                {"description": "a", "preferred_sources": ["CODE", "DOCUMENT"]},
                {"description": "b", "preferred_sources": ["DOCUMENT", "CODE"]},
            ]
        )
    )

    plan = subject.plan(QUESTION_PLAN)

    assert plan.requirements[0].preferred_sources == ("CODE", "DOCUMENT")
    assert plan.requirements[1].preferred_sources == ("DOCUMENT", "CODE")


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"items": []}',
        '{"requirements": [], "extra": 1}',
        '{"requirements": {}}',
        '{"requirements": [{"description": "a", "preferred_sources": ["CODE"]}]}',
        valid_plan(
            requirements=[
                {"description": "a", "preferred_sources": ["CODE"]},
                {"description": "b", "preferred_sources": ["CODE"]},
                {"description": "c", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(requirements=[{"description": "a"}]),
        valid_plan(
            requirements=[
                {"description": "a", "preferred_sources": ["CODE"], "extra": 1},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(
            requirements=[
                {"description": "", "preferred_sources": ["CODE"]},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(
            requirements=[
                {"description": "a\nb", "preferred_sources": ["CODE"]},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(
            requirements=[
                {"description": "```a```", "preferred_sources": ["CODE"]},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(
            requirements=[
                {"description": "x" * 301, "preferred_sources": ["CODE"]},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(
            requirements=[
                {"description": "a", "preferred_sources": []},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(
            requirements=[
                {"description": "a", "preferred_sources": ["DOC"]},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
        valid_plan(
            requirements=[
                {"description": "a", "preferred_sources": ["CODE", "CODE"]},
                {"description": "b", "preferred_sources": ["CODE"]},
            ]
        ),
    ],
)
def test_invalid_plan_falls_back_without_raising(response: str) -> None:
    subject, _ = planner(response)

    plan = subject.plan(QUESTION_PLAN)

    assert plan.decision_source == "fallback"
    assert plan.requirements == ()


def test_network_failure_falls_back_and_leaks_nothing() -> None:
    subject, _ = planner(RuntimeError("secret-key-value"))

    plan = subject.plan(QUESTION_PLAN)

    assert plan.decision_source == "fallback"
    assert "secret-key-value" not in json.dumps(plan.to_dict(), ensure_ascii=False)


def test_missing_client_factory_falls_back() -> None:
    plan = EvidencePlanner().plan(QUESTION_PLAN)

    assert plan.decision_source == "fallback"


def test_empty_question_plan_falls_back() -> None:
    subject, client = planner(valid_plan())
    empty = QuestionPlan(
        original_query="q",
        intent_summary="i",
        sub_questions=(),
        answer_depth="standard",
        decision_source="llm",
    )

    plan = subject.plan(empty)

    assert plan.decision_source == "fallback"
    assert client.calls == []


def test_plan_serializes_requirement_ids() -> None:
    subject, _ = planner(valid_plan())

    payload = subject.plan(QUESTION_PLAN).to_dict()

    assert payload["decision_source"] == "llm"
    assert payload["requirements"][0] == {
        "id": "ER1",
        "sub_question_id": "SQ1",
        "description": "订单关闭的触发与入口实现",
        "preferred_sources": ["CODE"],
    }
