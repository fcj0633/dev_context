from __future__ import annotations

import time

from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.retrieval.service import RetrievalService
from devcontext.routing import QueryType, RouteDecision


class RetrievalPolicy:
    def __init__(self, service: RetrievalService) -> None:
        self.service = service

    def search(
        self, query: str, decision: RouteDecision, top_k: int
    ) -> list[SearchResult]:
        return self.search_with_trace(query, decision, top_k).results

    def search_with_trace(
        self, query: str, decision: RouteDecision, top_k: int
    ) -> SearchExecution:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")

        if decision.query_type is QueryType.CODE:
            return self.service.search_with_trace(
                "hybrid", query, top_k, source_type="CODE"
            )
        if decision.query_type is QueryType.DOC:
            return self.service.search_with_trace(
                "vector", query, top_k, source_type="DOCUMENT"
            )

        total_started = time.perf_counter()
        code = self.service.search_with_trace(
            "vector", query, top_k, source_type="CODE"
        )
        document = self.service.search_with_trace(
            "vector", query, top_k, source_type="DOCUMENT"
        )
        results = _interleave_results(code.results, document.results, top_k)
        timings = _combine_timings(code.timings, document.timings)
        measured_total = (time.perf_counter() - total_started) * 1000
        stage_total = (
            timings.query_embedding_ms
            + timings.keyword_sql_ms
            + timings.vector_sql_ms
            + timings.fusion_ms
        )
        timings.total_ms = max(measured_total, stage_total)
        return SearchExecution(results, timings)


def _interleave_results(
    code: list[SearchResult], document: list[SearchResult], top_k: int
) -> list[SearchResult]:
    merged: list[SearchResult] = []
    seen: set[int] = set()
    for index in range(max(len(code), len(document))):
        for results in (code, document):
            if index >= len(results):
                continue
            result = results[index]
            if result.id in seen:
                continue
            seen.add(result.id)
            merged.append(result)
            if len(merged) == top_k:
                return merged
    return merged


def _combine_timings(*values: SearchTimings) -> SearchTimings:
    return SearchTimings(
        query_embedding_ms=sum(value.query_embedding_ms for value in values),
        keyword_sql_ms=sum(value.keyword_sql_ms for value in values),
        vector_sql_ms=sum(value.vector_sql_ms for value in values),
        fusion_ms=sum(value.fusion_ms for value in values),
        total_ms=sum(value.total_ms for value in values),
    )
