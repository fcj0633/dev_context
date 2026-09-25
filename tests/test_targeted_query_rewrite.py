from __future__ import annotations

import json

import pytest

from devcontext.agentic import (
    MissingAspect,
    QueryRewriteError,
    SufficiencyResult,
    TargetedQueryRewriter,
)
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


def route() -> RouteDecision:
    return RouteDecision(QueryType.MIXED, DecisionSource.RULES, "test")


def context_bundle() -> ContextBundle:
    item = ContextItem(
        citation=Citation(
            label="C1",
            source_type="DOCUMENT",
            file_path="design.md",
            heading_path=["购票", "责任链"],
        ),
        content="TrainPurchaseTicketParamStockChainFilter 负责参数校验",
        chunk_id=1,
        chunk_type="DOCUMENT_SECTION",
        score=1.0,
        retrieval_rank=1,
    )
    rendered = "[C1] DOCUMENT\nFile: design.md\n\n" + item.content
    return ContextBundle("原始问题", [item], rendered, len(rendered), 6000, False)


def insufficient(*sources: str) -> SufficiencyResult:
    return SufficiencyResult(
        enough=False,
        missing_aspects=tuple(
            MissingAspect(source, f"缺少 {source} 证据") for source in sources
        ),
        reason="证据不足",
        decision_source="rules",
    )


@pytest.mark.parametrize(
    ("sources", "expected_type"),
    [
        (("CODE",), QueryType.CODE),
        (("DOCUMENT",), QueryType.DOC),
        (("CODE", "DOCUMENT"), QueryType.MIXED),
    ],
)
def test_rewrite_target_is_derived_from_missing_sources(
    sources: tuple[str, ...], expected_type: QueryType
) -> None:
    client = FakeLLMClient(
        json.dumps({"rewritten_query": "精确的新检索查询"}, ensure_ascii=False)
    )
    rewriter = TargetedQueryRewriter(lambda: client)

    result = rewriter.rewrite(
        "原始问题", route(), insufficient(*sources), context_bundle()
    )

    assert result.target_query_type is expected_type
    assert result.rewritten_query == "精确的新检索查询"
    prompt = client.calls[0][1].content
    assert "Original Query:\n原始问题" in prompt
    assert "Original Route: MIXED" in prompt
    assert "Missing Aspects:" in prompt
    assert "TrainPurchaseTicketParamStockChainFilter" in prompt
    assert "Target Evidence Type" in prompt


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"query": "x"}',
        '{"rewritten_query": ""}',
        '{"rewritten_query": "原始问题"}',
        '{"rewritten_query": "```code```"}',
        '{"rewritten_query": "first\\nsecond"}',
    ],
)
def test_invalid_rewrite_is_rejected(response: str) -> None:
    with pytest.raises(QueryRewriteError):
        TargetedQueryRewriter(lambda: FakeLLMClient(response)).rewrite(
            "原始问题", route(), insufficient("CODE"), context_bundle()
        )


def test_rewrite_generation_failure_is_wrapped_safely() -> None:
    with pytest.raises(QueryRewriteError, match="generation failed") as captured:
        TargetedQueryRewriter(
            lambda: FakeLLMClient(RuntimeError("secret-key-value"))
        ).rewrite("原始问题", route(), insufficient("CODE"), context_bundle())

    assert "secret-key-value" not in str(captured.value)


def test_sufficient_context_cannot_be_rewritten() -> None:
    sufficient = SufficiencyResult(True, (), "足够", "llm")
    with pytest.raises(ValueError, match="requires insufficient"):
        TargetedQueryRewriter(lambda: FakeLLMClient("unused")).rewrite(
            "原始问题", route(), sufficient, context_bundle()
        )
