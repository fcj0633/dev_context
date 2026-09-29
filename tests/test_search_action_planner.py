from __future__ import annotations

import json

from devcontext.agentic import RequirementCoverage, SearchAction, SearchActionPlanner
from devcontext.planning import EvidenceRequirement


def requirement() -> EvidenceRequirement:
    return EvidenceRequirement(
        "ER1",
        "确认当前占座的数据库更新边界",
        "找到条件更新和影响行数检查",
        "CORE",
        "CURRENT",
        "CODE",
    )


class FakeClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = []

    def generate(self, messages) -> str:
        self.messages = messages
        return self.response


def test_search_action_query_only_exists_in_dynamic_action() -> None:
    response = json.dumps({
        "actions": [{
            "requirement_id": "ER1",
            "query": "seat_status AVAILABLE conditional update affected rows",
            "reason": "查找最终数据库并发边界",
        }]
    }, ensure_ascii=False)
    client = FakeClient(response)
    action = SearchActionPlanner(lambda: client).plan_actions(
        "如何保证占座一致性？", [requirement()], round_index=0
    )[0]

    assert action.query == "seat_status AVAILABLE conditional update affected rows"
    assert action.source_scope == "CODE"
    assert action.decision_source == "llm"
    assert "query" not in requirement().to_dict()
    assert "不回答用户问题" in client.messages[0].content


def test_fallback_followup_query_is_not_identical_to_previous_query() -> None:
    item = requirement()
    original = "如何保证占座一致性？"
    initial = SearchActionPlanner().plan_actions(
        original, [item], round_index=0
    )[0]
    coverage = RequirementCoverage(
        "ER1",
        "PARTIAL",
        (1,),
        ("尚缺影响行数检查",),
        "尚缺具体检查",
        "rules",
    )

    followup = SearchActionPlanner().plan_actions(
        original,
        [item],
        round_index=1,
        history=(initial,),
        coverage={"ER1": coverage},
    )[0]

    assert followup.query != initial.query
    assert followup.decision_source == "fallback"


def test_invalid_llm_action_falls_back_without_changing_requirement() -> None:
    client = FakeClient('{"actions":[{"requirement_id":"ER1","query":"x"}]}')
    action = SearchActionPlanner(lambda: client).plan_actions(
        "如何保证占座一致性？", [requirement()], round_index=0
    )[0]

    assert action.decision_source == "fallback"
    assert action.requirement_id == "ER1"
    assert action.source_scope == "CODE"


def test_followup_llm_action_must_use_a_discovered_project_term() -> None:
    response = json.dumps({
        "actions": [{
            "requirement_id": "ER1",
            "query": "SeatService lockSeat affected rows",
            "reason": "利用第一轮发现的方法精确补查",
        }]
    }, ensure_ascii=False)
    previous = SearchAction(
        "SA1", "ER1", 0, "占座一致性", "CODE", "首次查询", "llm"
    )
    coverage = RequirementCoverage(
        "ER1", "PARTIAL", (1,), ("尚缺影响行数检查",), "部分满足", "llm"
    )

    action = SearchActionPlanner(lambda: FakeClient(response)).plan_actions(
        "如何保证占座一致性？",
        [requirement()],
        round_index=1,
        history=(previous,),
        coverage={"ER1": coverage},
        discovered_terms={"ER1": ("lockSeat",)},
    )[0]

    assert action.decision_source == "llm"
    assert "lockSeat" in action.query
