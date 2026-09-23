from devcontext.embedding.client import BailianEmbeddingClient


def test_embedding_batch_limit_is_enforced() -> None:
    try:
        BailianEmbeddingClient("not-a-real-key", "https://example.invalid/v1", batch_size=11)
    except ValueError as exception:
        assert "batch_size" in str(exception)
    else:
        raise AssertionError("Expected invalid batch size to fail")
