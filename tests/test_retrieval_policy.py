from __future__ import annotations

import pytest

from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.retrieval.policy import (
    RetrievalPolicy,
    _metadata_terms,
    _promote_document_anchor,
)
from devcontext.routing import DecisionSource, QueryType, RouteDecision


def result(identifier: int, source_type: str) -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type=source_type,
        chunk_type="METHOD" if source_type == "CODE" else "DOCUMENT_SECTION",
        file_path=f"{identifier}.java" if source_type == "CODE" else f"{identifier}.md",
        content="content",
        start_line=1 if source_type == "CODE" else None,
        end_line=2 if source_type == "CODE" else None,
        class_name="Service" if source_type == "CODE" else None,
        symbol_name="run" if source_type == "CODE" else None,
        signature="void run()" if source_type == "CODE" else None,
        title="Design" if source_type == "DOCUMENT" else None,
        score=1.0,
    )


def decision(query_type: QueryType) -> RouteDecision:
    return RouteDecision(query_type, DecisionSource.RULES, "test")


class FakeRetrievalService:
    def __init__(
        self,
        code: list[SearchResult] | None = None,
        document: list[SearchResult] | None = None,
    ) -> None:
        self.code = code or []
        self.document = document or []
        self.calls: list[tuple[str, str, int, str | None]] = []

    def search_with_trace(
        self,
        strategy: str,
        query: str,
        top_k: int,
        *,
        source_type: str | None = None,
    ) -> SearchExecution:
        self.calls.append((strategy, query, top_k, source_type))
        results = self.code if source_type == "CODE" else self.document
        timings = SearchTimings(
            query_embedding_ms=2.0,
            keyword_sql_ms=3.0 if strategy == "hybrid" else 0.0,
            vector_sql_ms=5.0,
            fusion_ms=1.0 if strategy == "hybrid" else 0.0,
            total_ms=12.0,
        )
        return SearchExecution(results[:top_k], timings)


def test_code_and_doc_routes_select_fixed_filtered_strategies() -> None:
    service = FakeRetrievalService([result(1, "CODE")], [result(2, "DOCUMENT")])
    policy = RetrievalPolicy(service)  # type: ignore[arg-type]

    code = policy.search("代码在哪里", decision(QueryType.CODE), 5)
    document = policy.search("为什么这样设计", decision(QueryType.DOC), 5)

    assert [item.id for item in code] == [1]
    assert [item.id for item in document] == [2]
    assert service.calls == [
        ("hybrid", "代码在哪里", 5, "CODE"),
        ("vector", "为什么这样设计", 5, "DOCUMENT"),
    ]


def test_mixed_route_interleaves_sources_and_combines_timings() -> None:
    service = FakeRetrievalService(
        [result(1, "CODE"), result(2, "CODE"), result(3, "CODE")],
        [result(11, "DOCUMENT"), result(12, "DOCUMENT")],
    )
    policy = RetrievalPolicy(service)  # type: ignore[arg-type]

    execution = policy.search_with_trace("实现与设计", decision(QueryType.MIXED), 5)

    assert [item.id for item in execution.results] == [1, 11, 2, 12, 3]
    assert service.calls == [
        ("vector", "实现与设计", 5, "CODE"),
        ("vector", "实现与设计", 20, "DOCUMENT"),
    ]
    assert execution.timings.query_embedding_ms == 4.0
    assert execution.timings.vector_sql_ms == 10.0
    assert execution.timings.keyword_sql_ms == 0.0
    assert execution.timings.fusion_ms > 0.0
    assert execution.timings.total_ms >= 14.0
    assert execution.source_candidates == {
        "CODE": service.code,
        "DOCUMENT": service.document,
    }


def test_document_anchor_promotes_only_best_metadata_match_stably() -> None:
    first = result(11, "DOCUMENT")
    first.file_path = "docs/general.md"
    first.title = "系统说明"
    second = result(12, "DOCUMENT")
    second.file_path = "docs/payment.md"
    second.title = "支付回调"
    second.heading_path = ["支付模块", "通知推进流程"]
    third = result(13, "DOCUMENT")
    third.file_path = "docs/other.md"
    third.title = "其他设计"
    original_scores = [item.score for item in (first, second, third)]

    promoted = _promote_document_anchor(
        "payCallback 支付回调的通知推进流程", [first, second, third]
    )

    assert [item.id for item in promoted] == [12, 11, 13]
    assert [item.score for item in (first, second, third)] == original_scores
    assert _metadata_terms("PayCallback 支付回调") >= {
        "paycallback",
        "支付",
        "付回",
        "回调",
    }


def test_document_anchor_keeps_original_order_for_ties_or_no_terms() -> None:
    first = result(11, "DOCUMENT")
    second = result(12, "DOCUMENT")
    first.title = second.title = "支付设计"

    assert [item.id for item in _promote_document_anchor("支付", [first, second])] == [
        11,
        12,
    ]
    assert [item.id for item in _promote_document_anchor("!", [first, second])] == [
        11,
        12,
    ]


def test_mixed_document_anchor_uses_twenty_candidates_and_final_top_k() -> None:
    documents = [result(identifier, "DOCUMENT") for identifier in range(11, 31)]
    documents[11].title = "支付回调通知推进"
    service = FakeRetrievalService([result(1, "CODE")], documents)

    execution = RetrievalPolicy(service).search_with_trace(  # type: ignore[arg-type]
        "支付回调通知推进的实现与设计", decision(QueryType.MIXED), 5
    )

    assert service.calls[-1] == (
        "vector",
        "支付回调通知推进的实现与设计",
        20,
        "DOCUMENT",
    )
    assert len(execution.results) <= 5
    assert execution.source_candidates["DOCUMENT"][0].id == 22


def test_mixed_route_does_not_invent_a_missing_source_or_duplicate_ids() -> None:
    duplicate = result(1, "DOCUMENT")
    service = FakeRetrievalService(
        [result(1, "CODE"), result(2, "CODE")],
        [duplicate],
    )

    results = RetrievalPolicy(service).search(  # type: ignore[arg-type]
        "实现与设计", decision(QueryType.MIXED), 5
    )

    assert [item.id for item in results] == [1, 2]
    assert all(item.source_type == "CODE" for item in results)


def test_mixed_top_one_starts_with_code() -> None:
    service = FakeRetrievalService([result(1, "CODE")], [result(2, "DOCUMENT")])

    results = RetrievalPolicy(service).search(  # type: ignore[arg-type]
        "实现与设计", decision(QueryType.MIXED), 1
    )

    assert [item.id for item in results] == [1]


@pytest.mark.parametrize(("query", "top_k"), [(" ", 5), ("query", 0), ("query", 101)])
def test_policy_rejects_invalid_input(query: str, top_k: int) -> None:
    with pytest.raises(ValueError):
        RetrievalPolicy(FakeRetrievalService()).search(  # type: ignore[arg-type]
            query, decision(QueryType.CODE), top_k
        )
