from pathlib import Path

from devcontext.embedding.cache import EmbeddingCache


def test_embedding_cache_ignores_wrong_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "cache.jsonl"
    cache = EmbeddingCache(path, "model", 3)
    cache.append("first", [1.0, 2.0, 3.0])
    path.write_text(
        path.read_text(encoding="utf-8")
        + '{"key":"model:3:wrong","vector":[1.0]}\n',
        encoding="utf-8",
    )

    loaded = EmbeddingCache(path, "model", 3)

    assert loaded.get("first") == [1.0, 2.0, 3.0]
    assert loaded.get("wrong") is None
