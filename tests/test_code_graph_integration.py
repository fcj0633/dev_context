from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from pgvector.psycopg import register_vector
import pytest

from devcontext.code_graph.models import CodeSymbol, SymbolEdge
from devcontext.code_graph.store import CodeGraphStore
from devcontext.code_graph.query import CodeGraphQuery
from devcontext.config import Settings
from devcontext.models import Chunk
from devcontext.storage import ChunkStore

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("DEVCONTEXT_RUN_INTEGRATION") != "1", reason="requires PostgreSQL")]


@pytest.fixture
def snapshot():
    settings = Settings()
    repository = "graph-test-" + uuid4().hex
    chunks = [Chunk(repository, "CODE", "CLASS", "Service.java", "class Service {}", 1, 1)]
    symbols = [CodeSymbol(repository, "T:demo.Service", "CLASS", "Service", "demo.Service", None, None, 0, "Service.java", 1, 1)]
    for index in range(16):
        chunks.append(Chunk(repository, "CODE", "METHOD", "Service.java", f"void f{index}() {{}}", index + 2, index + 2,
                            class_name="Service", symbol_name=f"f{index}", signature=f"void f{index}()"))
        symbols.append(CodeSymbol(repository, f"M:demo.Service#f{index}()", "METHOD", f"f{index}", f"demo.Service#f{index}",
                                  f"f{index}()", "T:demo.Service", index + 1, "Service.java", index + 2, index + 2))
    edges = []
    for source, start in ((0, 2), (1, 8)):
        for target in range(start, start + 6):
            for column in (1, 10):
                edges.append(SymbolEdge(repository, f"M:demo.Service#f{source}()", f"M:demo.Service#f{target}()",
                                        "CALLS", source + 2, column, "SYMBOL_SOLVER_EXACT"))
    vectors = [[0.01] * settings.embedding_dimensions for _ in chunks]
    store = ChunkStore(settings.database_url)
    store.initialize(); store.initialize()
    store.replace_repository_snapshot(repository, chunks, vectors, symbols, edges, "fixture")
    try:
        yield settings, repository, chunks, symbols, edges, vectors, store
    finally:
        with store._connect(vectors=False) as connection:
            connection.execute("DELETE FROM knowledge_chunk WHERE repository = %s", (repository,))


def test_neighbors_are_partitioned_by_node_and_duplicate_calls_do_not_consume_limit(snapshot):
    settings, repository, *_ = snapshot
    graph = CodeGraphStore(settings.database_url, repository)
    with graph.session() as session:
        anchors = session.symbols_by_keys(["M:demo.Service#f0()", "M:demo.Service#f1()"])
        neighbors = session.neighbors([a["id"] for a in anchors], ("CALLS",), (), limit=5)
        assert [len(neighbors[a["id"]]) for a in anchors] == [5, 5]
        assert all(len({n["neighbor_id"] for n in rows}) == 5 for rows in neighbors.values())
        assert len(session.find_symbols(["Service.f0"])) == 1
        assert len(session.find_symbols(["demo.Service#f0()"])) == 1
        assert len(session.find_symbols(["demo.Service.f0()"])) == 1
        assert session.chunks_for_symbols(anchors)[0].source_type == "CODE"
    query = CodeGraphQuery(graph)
    assert len(query.query("callees", "M:demo.Service#f0()")["relations"]) == 5
    with pytest.raises(ValueError):
        query.query("callees", "f0")


def test_snapshot_replacement_rollback_and_old_api_cascade(snapshot):
    settings, repository, chunks, symbols, edges, vectors, store = snapshot
    with store._connect(vectors=False) as connection:
        old_ids = [r["id"] for r in connection.execute("SELECT id FROM knowledge_chunk WHERE repository = %s ORDER BY id", (repository,))]

    class FailingConnection(psycopg.Connection):
        def execute(self, query, params=None, **kwargs):
            if "INSERT INTO code_symbol(" in str(query):
                raise RuntimeError("injected failure after inserting new chunks")
            return super().execute(query, params, **kwargs)

    failing_store = ChunkStore(settings.database_url)
    def connect(**kwargs):
        connection = FailingConnection.connect(settings.database_url, row_factory=dict_row)
        register_vector(connection)
        return connection
    failing_store._connect = connect
    with pytest.raises(RuntimeError, match="injected failure"):
        failing_store.replace_repository_snapshot(repository, chunks, vectors, symbols, edges, "fixture")
    with store._connect(vectors=False) as connection:
        assert [r["id"] for r in connection.execute("SELECT id FROM knowledge_chunk WHERE repository = %s ORDER BY id", (repository,))] == old_ids
        assert connection.execute("SELECT count(*) AS n FROM code_symbol_edge WHERE repository = %s", (repository,)).fetchone()["n"] == len(edges)
    store.replace_repository_snapshot(repository, chunks, vectors, symbols, edges, "fixture")
    with store._connect(vectors=False) as connection:
        assert connection.execute("SELECT count(*) AS n FROM code_symbol WHERE repository = %s", (repository,)).fetchone()["n"] == len(symbols)
    store.replace_repository(repository, chunks, vectors, "fixture")
    with store._connect(vectors=False) as connection:
        assert connection.execute("SELECT count(*) AS n FROM code_symbol WHERE repository = %s", (repository,)).fetchone()["n"] == 0
        assert connection.execute("SELECT count(*) AS n FROM code_symbol_edge WHERE repository = %s", (repository,)).fetchone()["n"] == 0


def test_concurrent_rebuilds_are_serialized_and_other_repository_is_untouched(snapshot):
    settings, repository, chunks, symbols, edges, vectors, store = snapshot
    other = repository + "-other"
    store.replace_repository_snapshot(other, [replace(c, repository=other) for c in chunks], vectors,
                                      [replace(s, repository=other) for s in symbols], [replace(e, repository=other) for e in edges], "fixture")
    try:
        with store._connect(vectors=False) as connection:
            before = connection.execute("SELECT id FROM knowledge_chunk WHERE repository = %s ORDER BY id", (other,)).fetchall()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(store.replace_repository_snapshot, repository, chunks, vectors, symbols, edges, "fixture") for _ in range(2)]
            assert [future.result(timeout=15) for future in futures] == [len(chunks), len(chunks)]
        with store._connect(vectors=False) as connection:
            assert connection.execute("SELECT id FROM knowledge_chunk WHERE repository = %s ORDER BY id", (other,)).fetchall() == before
            assert connection.execute("SELECT count(*) AS n FROM code_symbol WHERE repository = %s", (repository,)).fetchone()["n"] == len(symbols)
            assert connection.execute("SELECT count(*) AS n FROM code_symbol_edge WHERE repository = %s", (repository,)).fetchone()["n"] == len(edges)
    finally:
        with store._connect(vectors=False) as connection:
            connection.execute("DELETE FROM knowledge_chunk WHERE repository = %s", (other,))
