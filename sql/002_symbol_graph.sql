CREATE TABLE IF NOT EXISTS code_symbol (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository TEXT NOT NULL,
    symbol_key TEXT NOT NULL,
    symbol_kind TEXT NOT NULL CHECK (symbol_kind IN ('CLASS', 'INTERFACE', 'METHOD', 'CONSTRUCTOR')),
    simple_name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    canonical_signature TEXT,
    owner_symbol_id BIGINT REFERENCES code_symbol(id) ON DELETE CASCADE,
    chunk_id BIGINT NOT NULL UNIQUE REFERENCES knowledge_chunk(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(repository, symbol_key),
    UNIQUE(repository, id),
    FOREIGN KEY(repository, owner_symbol_id) REFERENCES code_symbol(repository, id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS code_symbol_edge (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    repository TEXT NOT NULL,
    source_symbol_id BIGINT NOT NULL,
    target_symbol_id BIGINT NOT NULL,
    edge_type TEXT NOT NULL CHECK (edge_type IN ('EXTENDS', 'IMPLEMENTS', 'CALLS', 'CONSTRUCTS', 'OVERRIDES')),
    source_line INTEGER NOT NULL CHECK (source_line > 0),
    source_column INTEGER NOT NULL CHECK (source_column > 0),
    resolution_kind TEXT NOT NULL CHECK (resolution_kind IN ('AST_EXACT', 'SYMBOL_SOLVER_EXACT', 'DERIVED_EXACT')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(repository, source_symbol_id) REFERENCES code_symbol(repository, id) ON DELETE CASCADE,
    FOREIGN KEY(repository, target_symbol_id) REFERENCES code_symbol(repository, id) ON DELETE CASCADE,
    UNIQUE(repository, source_symbol_id, target_symbol_id, edge_type, source_line, source_column)
);
CREATE INDEX IF NOT EXISTS code_symbol_name_idx ON code_symbol(repository, simple_name);
CREATE INDEX IF NOT EXISTS code_symbol_qualified_idx ON code_symbol(repository, qualified_name);
CREATE INDEX IF NOT EXISTS code_symbol_signature_idx ON code_symbol(repository, canonical_signature);
CREATE INDEX IF NOT EXISTS code_symbol_owner_idx ON code_symbol(owner_symbol_id);
CREATE INDEX IF NOT EXISTS code_symbol_edge_source_idx ON code_symbol_edge(repository, source_symbol_id, edge_type);
CREATE INDEX IF NOT EXISTS code_symbol_edge_target_idx ON code_symbol_edge(repository, target_symbol_id, edge_type);
