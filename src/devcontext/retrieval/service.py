from __future__ import annotations

import time

from devcontext.config import Settings
from devcontext.embedding.client import BailianEmbeddingClient
from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.retrieval.hybrid import reciprocal_rank_fusion
from devcontext.storage import ChunkStore


class RetrievalService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = ChunkStore(settings.database_url)

    def search(self, strategy: str, query: str, top_k: int = 10) -> list[SearchResult]:
        return self.search_with_trace(strategy, query, top_k).results

    def search_with_trace(
        self, strategy: str, query: str, top_k: int = 10
    ) -> SearchExecution:
        total_started = time.perf_counter()
        if not query.strip():
            raise ValueError("Query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")
        if strategy not in {"keyword", "vector", "hybrid"}:
            raise ValueError(f"Unknown strategy: {strategy}")

        timings = SearchTimings()
        if strategy == "keyword":
            started = time.perf_counter()
            results = self.store.keyword_search(self.settings.repository_name, query, top_k)
            timings.keyword_sql_ms = _elapsed_ms(started)
            timings.total_ms = _elapsed_ms(total_started)
            return SearchExecution(results, timings)

        started = time.perf_counter()
        client = BailianEmbeddingClient(
            api_key=self.settings.api_key(),
            base_url=self.settings.dashscope_base_url,
            model=self.settings.embedding_model,
            dimensions=self.settings.embedding_dimensions,
            transport=self.settings.embedding_transport,
        )
        query_vector = client.embed_query(query)
        timings.query_embedding_ms = _elapsed_ms(started)
        if strategy == "vector":
            started = time.perf_counter()
            results = self.store.vector_search(
                self.settings.repository_name, query_vector, top_k
            )
            timings.vector_sql_ms = _elapsed_ms(started)
            timings.total_ms = _elapsed_ms(total_started)
            return SearchExecution(results, timings)

        pool_size = max(20, top_k)
        started = time.perf_counter()
        keyword = self.store.keyword_search(
            self.settings.repository_name, query, pool_size
        )
        timings.keyword_sql_ms = _elapsed_ms(started)
        started = time.perf_counter()
        vector = self.store.vector_search(
            self.settings.repository_name, query_vector, pool_size
        )
        timings.vector_sql_ms = _elapsed_ms(started)
        started = time.perf_counter()
        results = reciprocal_rank_fusion([keyword, vector], k=60, top_k=top_k)
        timings.fusion_ms = _elapsed_ms(started)
        timings.total_ms = _elapsed_ms(total_started)
        return SearchExecution(results, timings)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
