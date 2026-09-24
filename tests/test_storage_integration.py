from __future__ import annotations

import os

import psycopg
import pytest

from devcontext.config import Settings
from devcontext.storage import ChunkStore


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("DEVCONTEXT_RUN_INTEGRATION") != "1",
        reason="set DEVCONTEXT_RUN_INTEGRATION=1 with the PostgreSQL container running",
    ),
]


def test_extensions_data_and_identifier_search_are_ready() -> None:
    settings = Settings()
    with psycopg.connect(settings.database_url) as connection:
        extensions = {
            row[0]
            for row in connection.execute(
                "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')"
            ).fetchall()
        }
        dimensions = connection.execute(
            "SELECT vector_dims(embedding) FROM knowledge_chunk LIMIT 1"
        ).fetchone()[0]

    assert extensions == {"vector", "pg_trgm"}
    assert dimensions == 1024
    counts = ChunkStore(settings.database_url).count_by_type(settings.repository_name)
    assert counts["CODE"] > 0
    assert counts["DOCUMENT"] > 0

    results = ChunkStore(settings.database_url).keyword_search(
        settings.repository_name,
        "PurchaseTicketTxService.doPurchaseInTransaction 的事务实现",
        5,
    )
    assert any(result.symbol_name == "doPurchaseInTransaction" for result in results)

    store = ChunkStore(settings.database_url)
    code_results = store.keyword_search(
        settings.repository_name,
        "PurchaseTicketTxService",
        5,
        source_type="CODE",
    )
    document_results = store.vector_search(
        settings.repository_name,
        [0.0] * settings.embedding_dimensions,
        5,
        source_type="DOCUMENT",
    )
    assert code_results and all(result.source_type == "CODE" for result in code_results)
    assert document_results and all(
        result.source_type == "DOCUMENT" for result in document_results
    )
