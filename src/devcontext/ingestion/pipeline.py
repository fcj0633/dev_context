from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from devcontext.config import Settings, project_root
from devcontext.embedding.client import BailianEmbeddingClient
from devcontext.embedding.cache import EmbeddingCache
from devcontext.ingestion.java_parser_runner import JavaParserRunner
from devcontext.ingestion.markdown_parser import parse_markdown_tree
from devcontext.storage import ChunkStore


@dataclass(slots=True)
class IngestionSummary:
    code_chunks: int
    document_chunks: int
    total_chunks: int
    stored_by_type: dict[str, int]


def ingest(settings: Settings) -> IngestionSummary:
    settings.validate_sources()
    artifact = project_root() / "artifacts" / "java-chunks.jsonl"
    code_chunks = JavaParserRunner().parse(
        settings.devcontext_code_root,
        artifact,
        settings.repository_name,
    )
    document_chunks = parse_markdown_tree(
        settings.devcontext_doc_root,
        settings.repository_name,
    )
    chunks = code_chunks + document_chunks
    if not code_chunks or not document_chunks:
        raise RuntimeError(
            f"Both source types are required: code={len(code_chunks)}, docs={len(document_chunks)}"
        )

    embedding_client = BailianEmbeddingClient(
        api_key=settings.api_key(),
        base_url=settings.dashscope_base_url,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        transport=settings.embedding_transport,
    )
    cache = EmbeddingCache(
        project_root() / "artifacts" / "embedding-cache.jsonl",
        settings.embedding_model,
        settings.embedding_dimensions,
    )
    embedding_texts = [chunk.embedding_text() for chunk in chunks]
    fingerprints = [sha256(text.encode("utf-8")).hexdigest() for text in embedding_texts]
    vectors: list[list[float] | None] = [cache.get(value) for value in fingerprints]
    missing_indexes = [index for index, vector in enumerate(vectors) if vector is None]
    missing_texts = [embedding_texts[index] for index in missing_indexes]

    def save_batch(start: int, batch_vectors: list[list[float]]) -> None:
        for offset, vector in enumerate(batch_vectors):
            chunk_index = missing_indexes[start + offset]
            vectors[chunk_index] = vector
            cache.append(fingerprints[chunk_index], vector)

    if missing_texts:
        embedding_client.embed_documents(missing_texts, on_batch=save_batch)
    completed_vectors = [vector for vector in vectors if vector is not None]
    if len(completed_vectors) != len(chunks):
        raise RuntimeError("Embedding cache did not produce one vector per chunk")

    store = ChunkStore(settings.database_url)
    store.initialize()
    store.replace_repository(
        settings.repository_name,
        chunks,
        completed_vectors,
        settings.embedding_model,
    )
    return IngestionSummary(
        code_chunks=len(code_chunks),
        document_chunks=len(document_chunks),
        total_chunks=len(chunks),
        stored_by_type=store.count_by_type(settings.repository_name),
    )
