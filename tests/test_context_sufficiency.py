from __future__ import annotations

import json

import pytest

from devcontext.agentic import ContextSufficiencyChecker
from devcontext.llm import LLMMessage
from devcontext.models import Citation, ContextBundle, ContextItem
from devcontext.routing import DecisionSource, QueryType, RouteDecision


class FakeLLMClient:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[list[LLMMessage]] = []

    def generate(self, messages: list[LLMMessage]) -> str:
        self.calls.append(list(messages))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def decision(query_type: QueryType) -> RouteDecision:
    return RouteDecision(query_type, DecisionSource.RULES, "test")


def bundle(*source_types: str) -> ContextBundle:
    items = []
    for index, source_type in enumerate(source_types, start=1):
        items.append(
            ContextItem(
                citation=Citation(
                    label=f"C{index}",
                    source_type=source_type,
                    file_path="Service.java" if source_type == "CODE" else "design.md",
                    class_name="Service" if source_type == "CODE" else None,
                    symbol_name="run" if source_type == "CODE" else None,
                    start_line=1 if source_type == "CODE" else None,
                    end_line=3 if source_type == "CODE" else None,
                    heading_path=[] if source_type == "CODE" else ["设计", "流程"],
                ),
                content="evidence",
                chunk_id=index,
                chunk_type="METHOD" if source_type == "CODE" else "DOCUMENT_SECTION",
                score=1.0,
                retrieval_rank=index,
            )
        )
    rendered = "\n\n".join(f"[{item.citation.label}] evidence" for item in items)
    return ContextBundle("问题", items, rendered, len(rendered), 6000, False)


@pytest.mark.parametrize(
    ("query_type", "sources", "missing"),
    [
        (QueryType.CODE, (), ["CODE"]),
        (QueryType.DOC, (), ["DOCUMENT"]),
        (QueryType.MIXED, ("DOCUMENT",), ["CODE"]),
        (QueryType.MIXED, ("CODE",), ["DOCUMENT"]),
    ],
)
def test_missing_required_source_is_rejected_without_llm(
    query_type: QueryType,
    sources: tuple[str, ...],
    missing: list[str],
) -> None:
    client = FakeLLMClient("must not be called")
    checker = ContextSufficiencyChecker(lambda: client)

    result = checker.check("问题", decision(query_type), bundle(*sources))

    assert result.enough is False
    assert result.decision_source == "rules"
    assert [aspect.source_type for aspect in result.missing_aspects] == missing
    assert client.calls == []


def test_semantic_checker_receives_query_route_and_context() -> None:
    response = json.dumps(
        {
            "enough": False,
            "missing_aspects": [
                {"source_type": "CODE", "description": "命中了错误的方法"}
            ],
            "reason": "当前代码与问题不直接相关",
        },
        ensure_ascii=False,
    )
    client = FakeLLMClient(response)

    result = ContextSufficiencyChecker(lambda: client).check(
        "目标方法在哪里？", decision(QueryType.CODE), bundle("CODE")
    )

    assert result.enough is False
    assert result.decision_source == "llm"
    assert result.missing_aspects[0].description == "命中了错误的方法"
    assert len(client.calls) == 1
    assert "目标方法在哪里？" in client.calls[0][1].content
    assert "Required Evidence Type: CODE" in client.calls[0][1].content
    assert "[C1] evidence" in client.calls[0][1].content
    assert "只输出一个严格 JSON" in client.calls[0][0].content


def test_valid_enough_response_has_no_missing_aspects() -> None:
    client = FakeLLMClient(
        '{"enough": true, "missing_aspects": [], "reason": "证据直接覆盖问题"}'
    )

    result = ContextSufficiencyChecker(lambda: client).check(
        "实现与设计？", decision(QueryType.MIXED), bundle("CODE", "DOCUMENT")
    )

    assert result.enough is True
    assert result.missing_aspects == ()
    assert result.reason == "证据直接覆盖问题"


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"enough": true, "missing_aspects": [{"source_type": "CODE", "description": "x"}], "reason": "x"}',
        '{"enough": false, "missing_aspects": [], "reason": "x"}',
        '{"enough": false, "missing_aspects": [{"source_type": "DOCUMENT", "description": "x"}], "reason": "x"}',
        '{"enough": false, "missing_aspects": [{"source_type": "CODE", "description": ""}], "reason": "x"}',
    ],
)
def test_invalid_semantic_response_fails_closed(response: str) -> None:
    result = ContextSufficiencyChecker(lambda: FakeLLMClient(response)).check(
        "代码问题", decision(QueryType.CODE), bundle("CODE")
    )

    assert result.enough is False
    assert result.decision_source == "fallback"
    assert [aspect.source_type for aspect in result.missing_aspects] == ["CODE"]


def test_network_failure_fails_closed_without_leaking_exception() -> None:
    result = ContextSufficiencyChecker(
        lambda: FakeLLMClient(RuntimeError("secret-key-value"))
    ).check("代码问题", decision(QueryType.CODE), bundle("CODE"))

    assert result.enough is False
    assert result.decision_source == "fallback"
    assert "secret-key-value" not in result.reason


def test_empty_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="query must not be empty"):
        ContextSufficiencyChecker().check(" ", decision(QueryType.CODE), bundle())
