from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from devcontext.llm import LLMMessage
from devcontext.planning import QuestionPlanner
from devcontext.planning.question_planner import (
    MAX_SUB_QUESTIONS,
    QUESTION_PLANNER_SYSTEM_PROMPT,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLANNING_DIR = PROJECT_ROOT / "src" / "devcontext" / "planning"

QUERY = "详细解释用户注册的整个业务流程"


class FakeLLMClient:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[list[LLMMessage]] = []

    def generate(self, messages: list[LLMMessage]) -> str:
        self.calls.append(list(messages))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def planner(response: str | Exception) -> tuple[QuestionPlanner, FakeLLMClient]:
    client = FakeLLMClient(response)
    return QuestionPlanner(lambda: client), client


def sub_question(**overrides: object) -> dict[str, object]:
    """A sub-question that satisfies every rule, so each case isolates one violation."""
    entry: dict[str, object] = {
        "question": "注册入口在哪个类",
        "purpose": "确定流程起点",
        "evidence_description": "注册入口的实现代码",
        "preferred_sources": ["CODE"],
        "retrieval_query": "注册入口在哪个类 注册入口的实现代码",
        "importance": "CORE",
        "temporal_scope": "CURRENT",
    }
    entry.update(overrides)
    return entry


def plan_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "intent_summary": "用户想了解注册流程的整体实现",
        "sub_questions": [
            sub_question(),
            sub_question(
                question="参数校验在哪一步完成",
                purpose="确认校验位置",
                evidence_description="参数校验环节的校验规则与实现",
                preferred_sources=["DOCUMENT"],
            ),
        ],
        "answer_depth": "detailed",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_valid_plan_is_accepted() -> None:
    subject, client = planner(plan_payload())

    plan = subject.plan(QUERY)

    assert plan.decision_source == "llm"
    assert plan.original_query == QUERY
    assert plan.answer_depth == "detailed"
    assert [item.id for item in plan.sub_questions] == ["SQ1", "SQ2"]
    assert plan.sub_questions[0].question == "注册入口在哪个类"
    assert plan.sub_questions[0].purpose == "确定流程起点"
    assert plan.sub_questions[0].evidence_description == "注册入口的实现代码"
    assert plan.sub_questions[0].preferred_sources == ("CODE",)
    assert plan.sub_questions[1].preferred_sources == ("DOCUMENT",)
    assert client.calls[0][0].content == QUESTION_PLANNER_SYSTEM_PROMPT
    assert QUERY in client.calls[0][1].content


def test_investigation_plan_fields_are_preserved() -> None:
    subject, _ = planner(plan_payload(
        answer_goal="读者理解注册链路与边界",
        explanation_strategy="flow",
    ))

    plan = subject.plan(QUERY)

    assert plan.answer_goal == "读者理解注册链路与边界"
    assert plan.explanation_strategy == "flow"
    assert plan.sub_questions[0].retrieval_query == "注册入口在哪个类 注册入口的实现代码"
    assert plan.sub_questions[0].importance == "CORE"
    assert plan.sub_questions[0].temporal_scope == "CURRENT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"explanation_strategy": "unknown", "answer_goal": "目标"},
        {
            "answer_goal": "目标",
            "explanation_strategy": "flow",
            "sub_questions": [sub_question(importance="PRIMARY")],
        },
        {
            "answer_goal": "目标",
            "explanation_strategy": "flow",
            "sub_questions": [sub_question(temporal_scope="PAST")],
        },
    ],
)
def test_invalid_investigation_fields_fall_back(overrides: dict[str, object]) -> None:
    subject, _ = planner(plan_payload(**overrides))

    assert subject.plan(QUERY).decision_source == "fallback"


def test_both_sources_are_allowed_and_order_is_kept() -> None:
    subject, _ = planner(
        plan_payload(
            sub_questions=[
                sub_question(preferred_sources=["CODE", "DOCUMENT"]),
                sub_question(
                    question="参数校验在哪一步完成",
                    preferred_sources=["DOCUMENT", "CODE"],
                ),
            ]
        )
    )

    plan = subject.plan(QUERY)

    assert plan.sub_questions[0].preferred_sources == ("CODE", "DOCUMENT")
    assert plan.sub_questions[1].preferred_sources == ("DOCUMENT", "CODE")


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"intent_summary": "a", "sub_questions": [], "answer_depth": "standard"}',
        plan_payload(
            sub_questions=[sub_question()], answer_depth="standard", confidence=1
        ),
        plan_payload(sub_questions=[sub_question()], answer_depth="huge"),
        '{"intent_summary": "a", "sub_questions": {}, "answer_depth": "standard"}',
        plan_payload(sub_questions=[]),
        plan_payload(
            sub_questions=[
                sub_question(question=f"问题{index}")
                for index in range(MAX_SUB_QUESTIONS + 1)
            ]
        ),
        plan_payload(sub_questions=[{"question": "q", "purpose": "p"}]),
        plan_payload(
            sub_questions=[
                sub_question(),
                sub_question(evidence_description=""),
            ]
        ),
        plan_payload(sub_questions=[sub_question(question="")]),
        plan_payload(sub_questions=[sub_question(purpose="")]),
        plan_payload(sub_questions=[sub_question(evidence_description="")]),
        plan_payload(sub_questions=[sub_question(preferred_sources=[])]),
        plan_payload(sub_questions=[sub_question(preferred_sources=["DOC"])]),
        plan_payload(
            sub_questions=[sub_question(preferred_sources=["CODE", "CODE"])]
        ),
        plan_payload(
            sub_questions=[sub_question(), sub_question(question="注册入口在哪个类")]
        ),
        plan_payload(sub_questions=[sub_question(question="```q```")]),
        plan_payload(sub_questions=[sub_question(question="q\nq2")]),
        plan_payload(sub_questions=[sub_question(question="x" * 301)]),
    ],
)
def test_invalid_plan_falls_back_without_raising(response: str) -> None:
    subject, _ = planner(response)

    plan = subject.plan(QUERY)

    assert plan.decision_source == "fallback"
    assert len(plan.sub_questions) == 1
    assert plan.sub_questions[0].question == QUERY
    assert plan.answer_depth == "standard"


def test_network_failure_falls_back_and_leaks_nothing() -> None:
    subject, _ = planner(RuntimeError("secret-key-value"))

    plan = subject.plan(QUERY)

    assert plan.decision_source == "fallback"
    assert "secret-key-value" not in json.dumps(plan.to_dict(), ensure_ascii=False)


def test_missing_client_factory_falls_back() -> None:
    plan = QuestionPlanner().plan(QUERY)

    assert plan.decision_source == "fallback"
    assert plan.sub_questions[0].question == QUERY


def test_fallback_sub_question_keeps_both_sources() -> None:
    """The fallback plan is never routed from, but it must still be well-formed."""
    plan = QuestionPlanner().plan(QUERY)

    assert plan.sub_questions[0].preferred_sources == ("CODE", "DOCUMENT")


def test_empty_query_is_rejected() -> None:
    subject, _ = planner(plan_payload())

    with pytest.raises(ValueError):
        subject.plan("   ")


def test_sub_questions_are_capped_at_the_limit() -> None:
    subject, _ = planner(
        plan_payload(
            sub_questions=[
                sub_question(question=f"问题{index}")
                for index in range(MAX_SUB_QUESTIONS)
            ]
        )
    )

    plan = subject.plan(QUERY)

    assert len(plan.sub_questions) == MAX_SUB_QUESTIONS
    assert plan.sub_questions[-1].id == f"SQ{MAX_SUB_QUESTIONS}"


def test_plan_serializes_sources_as_a_list() -> None:
    subject, _ = planner(plan_payload(sub_questions=[sub_question()]))

    payload = subject.plan(QUERY).to_dict()

    assert payload["sub_questions"][0] == {
        "id": "SQ1",
        "question": "注册入口在哪个类",
        "purpose": "确定流程起点",
        "evidence_description": "注册入口的实现代码",
        "preferred_sources": ["CODE"],
        "retrieval_query": "注册入口在哪个类 注册入口的实现代码",
        "importance": "CORE",
        "temporal_scope": "CURRENT",
    }


def test_planning_package_never_imports_routing() -> None:
    for path in sorted(PLANNING_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("devcontext.routing"), path.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("devcontext.routing"), path.name
            elif isinstance(node, (ast.Name, ast.Attribute)):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert name != "QueryType", path.name


def test_planner_prompt_requires_the_evidence_source() -> None:
    # The planner now owns the source decision, so the prompt must ask for it.
    assert "必须为每条子问题说明它需要哪几类证据" in QUESTION_PLANNER_SYSTEM_PROMPT
    assert "CODE" in QUESTION_PLANNER_SYSTEM_PROMPT
    assert "DOCUMENT" in QUESTION_PLANNER_SYSTEM_PROMPT
    assert "不要输出 CODE、DOC、MIXED" not in QUESTION_PLANNER_SYSTEM_PROMPT


def test_planner_prompt_requires_project_directed_sub_questions() -> None:
    assert "子问题必须指向本项目的实现与设计" in QUESTION_PLANNER_SYSTEM_PROMPT
    assert "不要问成" in QUESTION_PLANNER_SYSTEM_PROMPT


def test_planner_prompt_keeps_the_dispatch_phrase() -> None:
    # tests/test_ask_cli.py dispatches fake planner calls on this phrase.
    assert "问题规划器" in QUESTION_PLANNER_SYSTEM_PROMPT
