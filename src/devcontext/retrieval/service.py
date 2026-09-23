from __future__ import annotations

from devcontext.config import Settings
from devcontext.embedding.client import BailianEmbeddingClient
from devcontext.models import SearchResult
from devcontext.retrieval.hybrid import reciprocal_rank_fusion
from devcontext.storage import ChunkStore


class RetrievalService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = ChunkStore(settings.database_url)

    def search(self, strategy: str, query: str, top_k: int = 10) -> list[SearchResult]:
        if not query.strip():
            raise ValueError("Query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")
        if strategy == "keyword":
            return self.store.keyword_search(self.settings.repository_name, query, top_k)
        client = BailianEmbeddingClient(
            api_key=self.settings.api_key(),
            base_url=self.settings.dashscope_base_url,
            model=self.settings.embedding_model,
            dimensions=self.settings.embedding_dimensions,
            transport=self.settings.embedding_transport,
        )
        query_vector = client.embed_query(query)
        if strategy == "vector":
            return self.store.vector_search(
                self.settings.repository_name, query_vector, top_k
            )
        if strategy == "hybrid":
            pool_size = max(20, top_k)
            keyword = self.store.keyword_search(
                self.settings.repository_name, query, pool_size
            )
            vector = self.store.vector_search(
                self.settings.repository_name, query_vector, pool_size
            )
            return reciprocal_rank_fusion([keyword, vector], k=60, top_k=top_k)
        raise ValueError(f"Unknown strategy: {strategy}")
