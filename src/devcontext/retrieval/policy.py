from __future__ import annotations

import re
import time
import unicodedata

from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.retrieval.service import RetrievalService
from devcontext.routing import DecisionSource, QueryType, RouteDecision


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
            execution = self.service.search_with_trace(
                "hybrid", query, top_k, source_type="CODE"
            )
            execution.source_candidates = {"CODE": execution.results}
            return execution
        if decision.query_type is QueryType.DOC:
            execution = self.service.search_with_trace(
                "vector", query, top_k, source_type="DOCUMENT"
            )
            execution.source_candidates = {"DOCUMENT": execution.results}
            return execution

        total_started = time.perf_counter()
        code = self.service.search_with_trace(
            "vector", query, top_k, source_type="CODE"
        )
        document_pool_size = max(20, top_k)
        document = self.service.search_with_trace(
            "vector", query, document_pool_size, source_type="DOCUMENT"
        )
        fusion_started = time.perf_counter()
        document_candidates = _promote_document_anchor(query, document.results)
        results = _interleave_results(code.results, document_candidates, top_k)
        timings = _combine_timings(code.timings, document.timings)
        timings.fusion_ms += (time.perf_counter() - fusion_started) * 1000
        measured_total = (time.perf_counter() - total_started) * 1000
        stage_total = (
            timings.query_embedding_ms
            + timings.keyword_sql_ms
            + timings.vector_sql_ms
            + timings.fusion_ms
        )
        timings.total_ms = max(measured_total, stage_total)
        return SearchExecution(
            results,
            timings,
            source_candidates={
                "CODE": code.results,
                "DOCUMENT": document_candidates,
            },
        )

    def search_scope_with_trace(
        self, query: str, source_scope: str, top_k: int
    ) -> SearchExecution:
        """Execute an evidence-controller action using existing retrieval tools.

        The LLM never selects a raw strategy.  Source scope is part of the
        evidence contract and this deterministic policy maps it to the retrieval
        behavior already benchmarked by the project.
        """
        if source_scope == "CODE":
            return self.search_with_trace(
                query,
                RouteDecision(
                    QueryType.CODE, DecisionSource.RULES, "evidence source scope CODE"
                ),
                top_k,
            )
        if source_scope == "DOCUMENT":
            return self.search_with_trace(
                query,
                RouteDecision(
                    QueryType.DOC,
                    DecisionSource.RULES,
                    "evidence source scope DOCUMENT",
                ),
                top_k,
            )
        if source_scope == "BOTH":
            return self.search_with_trace(
                query,
                RouteDecision(
                    QueryType.MIXED,
                    DecisionSource.RULES,
                    "evidence source scope BOTH",
                ),
                top_k,
            )
        if source_scope == "ANY":
            execution = self.service.search_with_trace(
                "hybrid", query, top_k, source_type=None
            )
            execution.source_candidates = {
                source: [
                    item for item in execution.results if item.source_type == source
                ]
                for source in ("CODE", "DOCUMENT")
            }
            return execution
        raise ValueError("source_scope must be CODE, DOCUMENT, BOTH, or ANY")


def _metadata_terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    terms = set(re.findall(r"[a-z_$][a-z0-9_$]{1,}", normalized))
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return terms


def _document_metadata_overlap(query_terms: set[str], result: SearchResult) -> float:
    if not query_terms:
        return 0.0
    metadata = " ".join(
        [result.file_path, result.title or "", *result.heading_path]
    )
    return len(query_terms & _metadata_terms(metadata)) / len(query_terms)


def _promote_document_anchor(
    query: str, results: list[SearchResult]
) -> list[SearchResult]:
    promoted = list(results)
    if len(promoted) < 2:
        return promoted
    query_terms = _metadata_terms(query)
    overlaps = [
        _document_metadata_overlap(query_terms, result) for result in promoted
    ]
    best_index = max(
        range(len(promoted)), key=lambda index: (overlaps[index], -index)
    )
    if best_index == 0 or overlaps[best_index] <= overlaps[0]:
        return promoted
    anchor = promoted.pop(best_index)
    promoted.insert(0, anchor)
    return promoted


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
