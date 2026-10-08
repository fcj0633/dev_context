from __future__ import annotations

from contextlib import contextmanager
import math

import psycopg
from psycopg.rows import dict_row

from devcontext.code_graph.models import EDGE_TYPES
from devcontext.deadline import remaining_seconds
from devcontext.models import SearchResult
from devcontext.storage import RESULT_COLUMNS

EDGE_ORDER_SQL = "CASE edge_type WHEN 'CALLS' THEN 0 WHEN 'CONSTRUCTS' THEN 1 WHEN 'OVERRIDES' THEN 2 WHEN 'IMPLEMENTS' THEN 3 ELSE 4 END"


class CodeGraphStore:
    def __init__(self, database_url: str, repository: str, timeout_seconds: float = 2.0):
        self.database_url = database_url
        self.repository = repository
        self.timeout_seconds = timeout_seconds

    @contextmanager
    def session(self):
        remaining = remaining_seconds()
        timeout = self.timeout_seconds if remaining is None else min(self.timeout_seconds, remaining)
        with psycopg.connect(
            self.database_url, row_factory=dict_row,
            connect_timeout=max(1, math.ceil(timeout)),
            options=f"-c statement_timeout={max(1, int(timeout * 1000))}",
        ) as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield CodeGraphSession(connection, self.repository, self.timeout_seconds)


class CodeGraphSession:
    def __init__(self, connection, repository, timeout_seconds):
        self.connection = connection
        self.repository = repository
        self.timeout_seconds = timeout_seconds

    def _execute(self, sql, parameters):
        remaining = remaining_seconds()
        timeout = self.timeout_seconds if remaining is None else min(self.timeout_seconds, remaining)
        self.connection.execute("SELECT set_config('statement_timeout', %s, true)", (str(max(1, int(timeout * 1000))),))
        return self.connection.execute(sql, parameters)

    def find_symbols(self, names: list[str], limit: int = 20) -> list[dict]:
        if not names:
            return []
        return self._execute("""
            SELECT s.*, c.file_path, c.start_line, c.end_line, min(CASE
                WHEN s.symbol_key = q.name THEN 0
                WHEN s.qualified_name || coalesce(substr(s.canonical_signature,
                    strpos(s.canonical_signature, '(')), '') = q.name THEN 1
                WHEN s.qualified_name = q.name OR replace(s.qualified_name, '#', '.') = q.name THEN 2
                WHEN owner.simple_name || '#' || s.simple_name = replace(q.name, '.', '#') THEN 3
                WHEN s.canonical_signature = q.name THEN 4 ELSE 5 END) AS match_rank
            FROM code_symbol s
            JOIN knowledge_chunk c ON c.id = s.chunk_id AND c.repository = s.repository
            LEFT JOIN code_symbol owner ON owner.id = s.owner_symbol_id
            CROSS JOIN unnest(%s::text[]) q(name)
            WHERE s.repository = %s AND (
                s.symbol_key = q.name OR s.qualified_name = q.name OR s.simple_name = q.name
                OR replace(s.qualified_name, '#', '.') = q.name
                OR s.canonical_signature = q.name
                OR s.qualified_name || coalesce(substr(s.canonical_signature,
                    strpos(s.canonical_signature, '(')), '') = q.name
                OR replace(s.qualified_name, '#', '.') || coalesce(substr(s.canonical_signature,
                    strpos(s.canonical_signature, '(')), '') = q.name
                OR owner.simple_name || '#' || s.simple_name = replace(q.name, '.', '#'))
            GROUP BY s.id, c.id ORDER BY match_rank, s.symbol_key LIMIT %s
        """, (names, self.repository, limit)).fetchall()

    def has_symbols(self) -> bool:
        return self._execute("SELECT EXISTS(SELECT 1 FROM code_symbol WHERE repository = %s) AS indexed",
                             (self.repository,)).fetchone()["indexed"]

    def symbols_for_chunks(self, chunk_ids: list[int]) -> list[dict]:
        if not chunk_ids:
            return []
        rows = self._execute("SELECT s.*, c.file_path, c.start_line, c.end_line FROM code_symbol s JOIN knowledge_chunk c ON c.id = s.chunk_id WHERE s.repository = %s AND s.chunk_id = ANY(%s)",
                             (self.repository, chunk_ids)).fetchall()
        rank = {value: index for index, value in enumerate(chunk_ids)}
        return sorted(rows, key=lambda row: (rank[row["chunk_id"]], row["symbol_key"]))

    def symbols_by_keys(self, keys: list[str]) -> list[dict]:
        return self._execute("SELECT s.*, c.file_path, c.start_line, c.end_line FROM code_symbol s JOIN knowledge_chunk c ON c.id = s.chunk_id WHERE s.repository = %s AND s.symbol_key = ANY(%s) ORDER BY s.symbol_key",
                             (self.repository, keys)).fetchall()

    def induced_relations(self, symbol_ids: list[int], edge_types: tuple[str, ...]) -> list[dict]:
        """Only edges between already retrieved endpoints; never discover neighbors."""
        if not symbol_ids or not edge_types:
            return []
        if not set(edge_types) <= EDGE_TYPES:
            raise ValueError('Invalid physical edge filter')
        return self._execute('''
            SELECT e.*, s.symbol_key AS source, t.symbol_key AS target,
                s.chunk_id AS source_chunk_id, t.chunk_id AS target_chunk_id
            FROM code_symbol_edge e
            JOIN code_symbol s ON s.id=e.source_symbol_id AND s.repository=e.repository
            JOIN code_symbol t ON t.id=e.target_symbol_id AND t.repository=e.repository
            WHERE e.repository=%s AND e.source_symbol_id=ANY(%s)
                AND e.target_symbol_id=ANY(%s) AND e.edge_type=ANY(%s::text[])
            ORDER BY s.symbol_key,t.symbol_key,e.edge_type,e.source_line,e.source_column
        ''', (self.repository, symbol_ids, symbol_ids, list(edge_types))).fetchall()

    def neighbors(self, node_ids: list[int], outgoing: tuple[str, ...], incoming: tuple[str, ...], limit: int = 5) -> dict[int, list[dict]]:
        if not node_ids:
            return {}
        if not set(outgoing + incoming) <= EDGE_TYPES or not 1 <= limit <= 100:
            raise ValueError("Invalid graph neighbor query")
        # Partition AFTER deduplicating call sites: five distinct neighbors for
        # EACH frontier node, never five rows shared by the entire frontier.
        rows = self._execute(f"""
            WITH links AS (
                SELECT source_symbol_id AS node_id, target_symbol_id AS neighbor_id,
                    edge_type, source_line, source_column, resolution_kind, 'outgoing' AS direction
                FROM code_symbol_edge WHERE repository = %s
                    AND source_symbol_id = ANY(%s) AND edge_type = ANY(%s::text[])
                UNION ALL
                SELECT target_symbol_id, source_symbol_id, edge_type, source_line, source_column,
                    resolution_kind, 'incoming'
                FROM code_symbol_edge WHERE repository = %s
                    AND target_symbol_id = ANY(%s) AND edge_type = ANY(%s::text[])
            ), distinct_neighbors AS (
                SELECT DISTINCT ON (node_id, neighbor_id) * FROM links
                ORDER BY node_id, neighbor_id, {EDGE_ORDER_SQL}, source_line, source_column, direction
            ), ranked AS (
                SELECT distinct_neighbors.*, s.id, s.repository, s.symbol_key, s.symbol_kind,
                    s.simple_name, s.qualified_name, s.canonical_signature, s.chunk_id,
                    c.file_path, c.start_line, c.end_line,
                    row_number() OVER (PARTITION BY node_id
                    ORDER BY {EDGE_ORDER_SQL}, s.symbol_key, source_line, source_column) AS neighbor_rank
                FROM distinct_neighbors JOIN code_symbol s ON s.id = neighbor_id AND s.repository = %s
                    JOIN knowledge_chunk c ON c.id = s.chunk_id
            )
            SELECT * FROM ranked WHERE neighbor_rank <= %s ORDER BY node_id, neighbor_rank
        """, (self.repository, node_ids, list(outgoing), self.repository, node_ids, list(incoming),
              self.repository, limit)).fetchall()
        grouped = {node_id: [] for node_id in node_ids}
        for row in rows:
            row["id"] = row["neighbor_id"]
            grouped[row["node_id"]].append(row)
        return grouped

    def tool_relations(self, node_ids: list[int], outgoing: tuple[str, ...], incoming: tuple[str, ...], limit: int = 5) -> dict[int, list[dict]]:
        """Bound distinct neighbors per node while retaining edge types/directions.

        This is deliberately separate from neighbors(), whose automatic
        expansion contract selects one representative edge per node pair.
        Multiple call sites of the same relation use the first source location.
        """
        if not node_ids:
            return {}
        if not set(outgoing + incoming) <= EDGE_TYPES or not 1 <= limit <= 100:
            raise ValueError("Invalid tool relationship query")
        rows = self._execute(f"""
            WITH links AS (
                SELECT source_symbol_id AS node_id, target_symbol_id AS neighbor_id,
                    edge_type, source_line, source_column, resolution_kind, 'outgoing' AS direction
                FROM code_symbol_edge WHERE repository = %s
                    AND source_symbol_id = ANY(%s) AND edge_type = ANY(%s::text[])
                UNION ALL
                SELECT target_symbol_id, source_symbol_id, edge_type, source_line, source_column,
                    resolution_kind, 'incoming'
                FROM code_symbol_edge WHERE repository = %s
                    AND target_symbol_id = ANY(%s) AND edge_type = ANY(%s::text[])
            ), relations AS (
                SELECT DISTINCT ON (node_id, neighbor_id, edge_type, direction) * FROM links
                ORDER BY node_id, neighbor_id, edge_type, direction, source_line, source_column
            ), neighbor_priority AS (
                SELECT node_id, neighbor_id, min({EDGE_ORDER_SQL}) AS edge_priority
                FROM relations GROUP BY node_id, neighbor_id
            ), ranked AS (
                SELECT p.*, s.symbol_key,
                    row_number() OVER (PARTITION BY node_id ORDER BY edge_priority, s.symbol_key) AS neighbor_rank
                FROM neighbor_priority p JOIN code_symbol s ON s.id = p.neighbor_id AND s.repository = %s
            )
            SELECT r.*, s.id, s.repository, s.symbol_key, s.symbol_kind, s.simple_name,
                s.qualified_name, s.canonical_signature, s.chunk_id,
                c.file_path, c.start_line, c.end_line, n.neighbor_rank
            FROM ranked n JOIN relations r USING (node_id, neighbor_id)
                JOIN code_symbol s ON s.id = r.neighbor_id AND s.repository = %s
                JOIN knowledge_chunk c ON c.id = s.chunk_id AND c.repository = s.repository
            WHERE n.neighbor_rank <= %s
            ORDER BY r.node_id, n.neighbor_rank, r.edge_type, r.direction, r.source_line, r.source_column
        """, (self.repository, node_ids, list(outgoing), self.repository, node_ids, list(incoming),
              self.repository, self.repository, limit)).fetchall()
        grouped = {node_id: [] for node_id in node_ids}
        for row in rows:
            row["id"] = row["neighbor_id"]
            grouped[row["node_id"]].append(row)
        return grouped

    def chunks_for_symbols(self, symbols: list[dict]) -> list[SearchResult]:
        if not symbols:
            return []
        ids = list(dict.fromkeys(row["chunk_id"] for row in symbols))
        rows = self._execute(f"SELECT {RESULT_COLUMNS}, 0.0::double precision AS score FROM knowledge_chunk WHERE repository = %s AND id = ANY(%s)",
                             (self.repository, ids)).fetchall()
        by_id = {row["id"]: SearchResult.from_row(row) for row in rows}
        return [by_id[value] for value in ids if value in by_id]
