CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('CODE', 'DOCUMENT')),
    chunk_type TEXT NOT NULL CHECK (
        chunk_type IN ('CLASS', 'INTERFACE', 'METHOD', 'CONSTRUCTOR', 'DOCUMENT_SECTION')
    ),
    file_path TEXT NOT NULL,
    module TEXT,
    package_name TEXT,
    class_name TEXT,
    symbol_name TEXT,
    signature TEXT,
    annotations TEXT[] NOT NULL DEFAULT '{}',
    javadoc TEXT,
    title TEXT,
    heading_path TEXT[] NOT NULL DEFAULT '{}',
    content TEXT NOT NULL,
    keyword_text TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    content_hash CHAR(64) NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding VECTOR(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Idempotency comes from the transaction-scoped repository replacement. A source line may
-- legitimately produce multiple chunks (for example, a single oversized Markdown line).
ALTER TABLE knowledge_chunk DROP CONSTRAINT IF EXISTS knowledge_chunk_location_unique;

CREATE INDEX IF NOT EXISTS knowledge_chunk_repository_idx
    ON knowledge_chunk (repository);
CREATE INDEX IF NOT EXISTS knowledge_chunk_source_type_idx
    ON knowledge_chunk (source_type);
CREATE INDEX IF NOT EXISTS knowledge_chunk_chunk_type_idx
    ON knowledge_chunk (chunk_type);
CREATE INDEX IF NOT EXISTS knowledge_chunk_file_path_idx
    ON knowledge_chunk (file_path);
CREATE INDEX IF NOT EXISTS knowledge_chunk_keyword_trgm_idx
    ON knowledge_chunk USING GIN (keyword_text gin_trgm_ops);
