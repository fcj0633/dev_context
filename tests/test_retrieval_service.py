from __future__ import annotations

from types import SimpleNamespace

import devcontext.retrieval.service as service_module
from devcontext.models import SearchResult
from devcontext.retrieval.service import RetrievalService


def result(identifier: int) -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type="CODE",
        chunk_type="METHOD",
        file_path="Service.java",
        content="content",
        start_line=1,
        end_line=2,
        class_name="Service",
        symbol_name="run",
        signature="void run()",
        title=None,
        score=1.0,
    )


class FakeStore:
    def keyword_search(self, repository: str, query: str, top_k: int) -> list[SearchResult]:
        return [result(1)]

    def vector_search(
        self, repository: str, query_embedding: list[float], top_k: int
    ) -> list[SearchResult]:
        return [result(1), result(2)]


class FakeEmbeddingClient:
    def __init__(self, **kwargs: object) -> None:
        pass

    def embed_query(self, query: str) -> list[float]:
        return [0.1, 0.2]


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        database_url="unused",
        repository_name="repo",
        dashscope_base_url="https://example.invalid",
        embedding_model="model",
        embedding_dimensions=2,
        embedding_transport="curl",
        api_key=lambda: "secret",
    )


def test_search_compatibility_and_hybrid_trace(monkeypatch) -> None:
    monkeypatch.setattr(service_module, "BailianEmbeddingClient", FakeEmbeddingClient)
    service = RetrievalService(settings())
    service.store = FakeStore()

    execution = service.search_with_trace("hybrid", "query", 5)
    compatible_results = service.search("keyword", "query", 5)

    assert [item.id for item in execution.results] == [1, 2]
    assert [item.id for item in compatible_results] == [1]
    assert execution.timings.query_embedding_ms >= 0
    assert execution.timings.keyword_sql_ms >= 0
    assert execution.timings.vector_sql_ms >= 0
    assert execution.timings.fusion_ms >= 0
    assert execution.timings.total_ms >= (
        execution.timings.query_embedding_ms
        + execution.timings.keyword_sql_ms
        + execution.timings.vector_sql_ms
        + execution.timings.fusion_ms
    )

    keyword = service.search_with_trace("keyword", "query", 5)
    vector = service.search_with_trace("vector", "query", 5)
    assert keyword.timings.query_embedding_ms == 0.0
    assert keyword.timings.vector_sql_ms == 0.0
    assert vector.timings.keyword_sql_ms == 0.0
    assert vector.timings.fusion_ms == 0.0


def test_unknown_strategy_fails_before_api_key_is_required() -> None:
    service = RetrievalService(settings())
    service.store = FakeStore()

    try:
        service.search_with_trace("unknown", "query", 5)
    except ValueError as exception:
        assert "Unknown strategy" in str(exception)
    else:
        raise AssertionError("unknown strategy should fail")


def test_source_filter_is_forwarded_without_changing_unfiltered_calls(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    class FilterAwareStore(FakeStore):
        def keyword_search(
            self,
            repository: str,
            query: str,
            top_k: int,
            *,
            source_type: str | None = None,
        ) -> list[SearchResult]:
            calls.append(("keyword", source_type))
            return [result(1)]

        def vector_search(
            self,
            repository: str,
            query_embedding: list[float],
            top_k: int,
            *,
            source_type: str | None = None,
        ) -> list[SearchResult]:
            calls.append(("vector", source_type))
            return [result(2)]

    monkeypatch.setattr(service_module, "BailianEmbeddingClient", FakeEmbeddingClient)
    service = RetrievalService(settings())
    service.store = FilterAwareStore()

    filtered = service.search("hybrid", "query", 5, source_type="CODE")
    service.search("keyword", "query", 5)

    assert filtered
    assert calls == [("keyword", "CODE"), ("vector", "CODE"), ("keyword", None)]


def test_invalid_source_filter_fails_before_api_key_is_required() -> None:
    service = RetrievalService(settings())
    service.store = FakeStore()

    try:
        service.search("vector", "query", 5, source_type="OTHER")
    except ValueError as exception:
        assert "source_type" in str(exception)
    else:
        raise AssertionError("invalid source type should fail")
