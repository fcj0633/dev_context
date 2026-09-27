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


def valid_plan(**overrides: object) -> str:
    payload = {
        "intent_summary": "用户想了解注册流程的整体实现",
        "sub_questions": [
            {"question": "注册入口在哪个类", "purpose": "确定流程起点"},
            {"question": "参数校验在哪一步完成", "purpose": "确认校验位置"},
        ],
        "answer_depth": "detailed",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_valid_plan_is_accepted() -> None:
    subject, client = planner(valid_plan())

    plan = subject.plan(QUERY)

    assert plan.decision_source == "llm"
    assert plan.original_query == QUERY
    assert plan.answer_depth == "detailed"
    assert [item.id for item in plan.sub_questions] == ["SQ1", "SQ2"]
    assert plan.sub_questions[0].question == "注册入口在哪个类"
    assert plan.sub_questions[0].purpose == "确定流程起点"
    assert client.calls[0][0].content == QUESTION_PLANNER_SYSTEM_PROMPT
    assert QUERY in client.calls[0][1].content


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"intent_summary": "a", "sub_questions": [], "answer_depth": "standard"}',
        '{"intent_summary": "a", "sub_questions": [{"question": "q", "purpose": "p"}], "answer_depth": "standard", "confidence": 1}',
        '{"intent_summary": "a", "sub_questions": [{"question": "q", "purpose": "p"}], "answer_depth": "huge"}',
        '{"intent_summary": "a", "sub_questions": {}, "answer_depth": "standard"}',
        '{"intent_summary": "a", "sub_questions": [], "answer_depth": "standard"}',
        json.dumps(
            {
                "intent_summary": "a",
                "sub_questions": [
                    {"question": f"q{index}", "purpose": "p"}
                    for index in range(MAX_SUB_QUESTIONS + 1)
                ],
                "answer_depth": "standard",
            },
            ensure_ascii=False,
        ),
        '{"intent_summary": "a", "sub_questions": [{"question": "q"}], "answer_depth": "standard"}',
        '{"intent_summary": "a", "sub_questions": [{"question": "", "purpose": "p"}], "answer_depth": "standard"}',
        '{"intent_summary": "a", "sub_questions": [{"question": "q", "purpose": ""}], "answer_depth": "standard"}',
        '{"intent_summary": "a", "sub_questions": [{"question": "q", "purpose": "p"}, {"question": "Q", "purpose": "p2"}], "answer_depth": "standard"}',
        '{"intent_summary": "a", "sub_questions": [{"question": "```q```", "purpose": "p"}], "answer_depth": "standard"}',
        '{"intent_summary": "a", "sub_questions": [{"question": "q\\nq2", "purpose": "p"}], "answer_depth": "standard"}',
        json.dumps(
            {
                "intent_summary": "a",
                "sub_questions": [{"question": "x" * 301, "purpose": "p"}],
                "answer_depth": "standard",
            },
            ensure_ascii=False,
        ),
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


def test_empty_query_is_rejected() -> None:
    subject, _ = planner(valid_plan())

    with pytest.raises(ValueError):
        subject.plan("   ")


def test_sub_questions_are_capped_at_the_limit() -> None:
    subject, _ = planner(
        json.dumps(
            {
                "intent_summary": "a",
                "sub_questions": [
                    {"question": f"问题{index}", "purpose": "p"}
                    for index in range(MAX_SUB_QUESTIONS)
                ],
                "answer_depth": "standard",
            },
            ensure_ascii=False,
        )
    )

    plan = subject.plan(QUERY)

    assert len(plan.sub_questions) == MAX_SUB_QUESTIONS
    assert plan.sub_questions[-1].id == f"SQ{MAX_SUB_QUESTIONS}"


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


def test_planner_prompt_forbids_source_labels() -> None:
    assert "不要输出 CODE、DOC、MIXED" in QUESTION_PLANNER_SYSTEM_PROMPT
    assert "不得出现用户问题中未提及的具体类名" in QUESTION_PLANNER_SYSTEM_PROMPT


def test_planner_prompt_requires_project_directed_sub_questions() -> None:
    assert "子问题必须指向本项目的实现与设计" in QUESTION_PLANNER_SYSTEM_PROMPT
    assert "不要问成" in QUESTION_PLANNER_SYSTEM_PROMPT


def test_planner_prompt_keeps_the_dispatch_phrase() -> None:
    # tests/test_ask_cli.py dispatches fake planner calls on this phrase.
    assert "问题规划器" in QUESTION_PLANNER_SYSTEM_PROMPT
