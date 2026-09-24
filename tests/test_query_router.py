from __future__ import annotations

from pathlib import Path

import pytest

from devcontext.evaluation.runner import load_cases
from devcontext.llm import LLMMessage
from devcontext.routing import DecisionSource, QueryRouter, QueryType


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeLLMClient:
    def __init__(self, response: str = "MIXED", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[list[LLMMessage]] = []

    def generate(self, messages: list[LLMMessage]) -> str:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("AuthGlobalFilter", QueryType.CODE),
        ("OrderServiceImpl.createTicketOrder 在哪里？", QueryType.CODE),
        ("@Transactional 注解在哪个方法？", QueryType.CODE),
        ("为什么支付事实和通知进度要分开保存？", QueryType.DOC),
        ("订单状态机如何设计？", QueryType.DOC),
        ("购票代码在哪里，相关设计依据是什么？", QueryType.MIXED),
    ],
)
def test_rule_router_classifies_typical_queries(
    query: str, expected: QueryType
) -> None:
    forbidden_calls = 0

    def forbidden_factory() -> FakeLLMClient:
        nonlocal forbidden_calls
        forbidden_calls += 1
        raise AssertionError("rule matches must not construct an LLM client")

    decision = QueryRouter(forbidden_factory).route(query)

    assert decision.query_type is expected
    assert decision.decision_source is DecisionSource.RULES
    assert forbidden_calls == 0


def test_domain_identifier_in_explanation_does_not_force_code_route() -> None:
    decision = QueryRouter().route(
        "Ticket 为什么预生成 orderSn，Order 为什么仍需要唯一索引？"
    )

    assert decision.query_type is QueryType.DOC
    assert decision.code_signals == ()


def test_rule_signal_order_and_serialization_are_stable() -> None:
    router = QueryRouter()
    first = router.route("OrderServiceImpl.createTicketOrder 的代码在哪里？")
    second = router.route("OrderServiceImpl.createTicketOrder 的代码在哪里？")

    assert first == second
    assert first.code_signals == (
        "qualified_java_symbol",
        "multi_word_camel_case",
        "code_or_source",
        "code_locator",
    )
    assert first.to_dict()["query_type"] == "CODE"
    assert first.to_dict()["decision_source"] == "rules"
    assert isinstance(first.to_dict()["code_signals"], list)


@pytest.mark.parametrize("response", ["CODE", "DOC", "MIXED"])
def test_ambiguous_query_uses_llm_fallback_with_strict_prompt(response: str) -> None:
    client = FakeLLMClient(response)
    factory_calls = 0

    def factory() -> FakeLLMClient:
        nonlocal factory_calls
        factory_calls += 1
        return client

    decision = QueryRouter(factory).route("帮我看看这个功能目前怎么样")

    assert factory_calls == 1
    assert len(client.calls) == 1
    assert decision.query_type is QueryType(response)
    assert decision.decision_source is DecisionSource.LLM
    assert "只能输出一个大写标签" in client.calls[0][0].content
    assert "帮我看看这个功能目前怎么样" in client.calls[0][1].content


@pytest.mark.parametrize(
    "response",
    ["code", "```CODE```", '{"query_type":"CODE"}', "CODE\n因为……", "UNKNOWN", ""],
)
def test_invalid_llm_output_falls_back_to_mixed(response: str) -> None:
    decision = QueryRouter(lambda: FakeLLMClient(response)).route("帮我看看")

    assert decision.query_type is QueryType.MIXED
    assert decision.decision_source is DecisionSource.FALLBACK
    assert decision.reason == "LLM fallback unavailable or invalid; defaulted to MIXED"


def test_missing_or_failed_llm_falls_back_without_raising() -> None:
    missing = QueryRouter().route("帮我看看")
    failed = QueryRouter(
        lambda: FakeLLMClient(error=RuntimeError("secret network details"))
    ).route("帮我看看")

    assert missing.decision_source is DecisionSource.FALLBACK
    assert failed.decision_source is DecisionSource.FALLBACK
    assert "secret" not in failed.reason


def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="query must not be empty"):
        QueryRouter().route("   ")


def test_checked_in_benchmark_is_classified_by_rules() -> None:
    cases = load_cases(PROJECT_ROOT / "benchmark" / "cases.jsonl")
    router = QueryRouter()
    decisions = [router.route(case["question"]) for case in cases]

    assert all(decision.decision_source is DecisionSource.RULES for decision in decisions)
    assert [decision.query_type.value for decision in decisions] == [
        case["type"] for case in cases
    ]
