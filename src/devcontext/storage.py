from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from devcontext.config import project_root
from devcontext.models import Chunk, SearchResult


RESULT_COLUMNS = """
    id, source_type, chunk_type, file_path, content, start_line, end_line,
    class_name, symbol_name, signature, title, annotations, heading_path
"""


class ChunkStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self, *, vectors: bool = True) -> psycopg.Connection[Any]:
        from devcontext.deadline import remaining_seconds
        import math
        remaining = remaining_seconds()
        options = {} if remaining is None else {
            "connect_timeout": max(1, math.ceil(remaining)),
            "options": f"-c statement_timeout={max(1, int(remaining * 1000))}",
        }
        connection = psycopg.connect(self.database_url, row_factory=dict_row, **options)
        if vectors:
            register_vector(connection)
        return connection

    def initialize(self) -> None:
        script_path = project_root() / "sql" / "001_schema.sql"
        script = script_path.read_text(encoding="utf-8")
        with self._connect(vectors=False) as connection:
            for statement in script.split(";"):
                if statement.strip():
                    connection.execute(statement)

    def replace_repository(
        self,
        repository: str,
        chunks: Sequence[Chunk],
        embeddings: Sequence[list[float]],
        embedding_model: str,
    ) -> int:
        if len(chunks) != len(embeddings):
            raise ValueError("Chunk and embedding counts differ")
        rows = []
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            rows.append(
                (
                    chunk.repository,
                    chunk.source_type,
                    chunk.chunk_type,
                    chunk.file_path,
                    chunk.module,
                    chunk.package_name,
                    chunk.class_name,
                    chunk.symbol_name,
                    chunk.signature,
                    chunk.annotations,
                    chunk.javadoc,
                    chunk.title,
                    chunk.heading_path,
                    chunk.content,
                    chunk.keyword_text(),
                    chunk.start_line,
                    chunk.end_line,
                    chunk.content_hash,
                    embedding_model,
                    embedding,
                )
            )
        sql = """
            INSERT INTO knowledge_chunk (
                repository, source_type, chunk_type, file_path, module, package_name,
                class_name, symbol_name, signature, annotations, javadoc, title,
                heading_path, content, keyword_text, start_line, end_line,
                content_hash, embedding_model, embedding
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        with self._connect() as connection:
            with connection.transaction():
                connection.execute(
                    "DELETE FROM knowledge_chunk WHERE repository = %s", (repository,)
                )
                with connection.cursor() as cursor:
                    cursor.executemany(sql, rows)
        return len(rows)

    def keyword_search(
        self,
        repository: str,
        query: str,
        top_k: int = 10,
        *,
        source_type: str | None = None,
    ) -> list[SearchResult]:
        _validate_source_type(source_type)
        identifiers = list(dict.fromkeys(re.findall(r"[A-Za-z_$][A-Za-z0-9_$]{2,}", query)))
        source_filter = "" if source_type is None else "AND source_type = %s"
        sql = f"""
            SELECT {RESULT_COLUMNS},
                (
                    CASE WHEN EXISTS (
                        SELECT 1 FROM unnest(%s::text[]) token
                        WHERE lower(coalesce(symbol_name, '')) = lower(token)
                    ) THEN 12.0 ELSE 0.0 END +
                    CASE WHEN EXISTS (
                        SELECT 1 FROM unnest(%s::text[]) token
                        WHERE lower(coalesce(class_name, '')) = lower(token)
                    ) THEN 10.0 ELSE 0.0 END +
                    CASE WHEN EXISTS (
                        SELECT 1 FROM unnest(%s::text[]) token
                        WHERE strpos(lower(coalesce(signature, '')), lower(token)) > 0
                    ) THEN 6.0 ELSE 0.0 END +
                    CASE WHEN strpos(lower(keyword_text), lower(%s)) > 0 THEN 2.0 ELSE 0.0 END +
                    similarity(keyword_text, %s)
                )::double precision AS score
            FROM knowledge_chunk
            WHERE repository = %s
              {source_filter}
              AND (
                    strpos(lower(keyword_text), lower(%s)) > 0
                    OR similarity(keyword_text, %s) > 0.03
                    OR EXISTS (
                        SELECT 1 FROM unnest(%s::text[]) token
                        WHERE strpos(lower(keyword_text), lower(token)) > 0
                    )
              )
            ORDER BY score DESC, id ASC
            LIMIT %s
        """
        parameters: list[Any] = [
            identifiers, identifiers, identifiers, query, query, repository,
        ]
        if source_type is not None:
            parameters.append(source_type)
        parameters.extend([query, query, identifiers, top_k])
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [SearchResult.from_row(row) for row in rows]

    def vector_search(
        self,
        repository: str,
        query_embedding: list[float],
        top_k: int = 10,
        *,
        source_type: str | None = None,
    ) -> list[SearchResult]:
        _validate_source_type(source_type)
        source_filter = "" if source_type is None else "AND source_type = %s"
        sql = f"""
            SELECT {RESULT_COLUMNS},
                (1 - (embedding <=> %s::vector))::double precision AS score
            FROM knowledge_chunk
            WHERE repository = %s
              {source_filter}
            ORDER BY embedding <=> %s::vector, id ASC
            LIMIT %s
        """
        parameters: list[Any] = [query_embedding, repository]
        if source_type is not None:
            parameters.append(source_type)
        parameters.extend([query_embedding, top_k])
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [SearchResult.from_row(row) for row in rows]

    def count_by_type(self, repository: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_type, count(*)::integer AS count
                FROM knowledge_chunk
                WHERE repository = %s
                GROUP BY source_type
                ORDER BY source_type
                """,
                (repository,),
            ).fetchall()
        return {row["source_type"]: row["count"] for row in rows}


def _validate_source_type(source_type: str | None) -> None:
    if source_type not in {None, "CODE", "DOCUMENT"}:
        raise ValueError("source_type must be CODE, DOCUMENT, or None")
