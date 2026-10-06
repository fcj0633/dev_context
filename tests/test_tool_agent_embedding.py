import pytest

from devcontext.deadline import RequestDeadlineExceeded
from devcontext.embedding.client import BailianEmbeddingClient, EmbeddingQueryError


def client_with_failure(monkeypatch, failure):
    client = BailianEmbeddingClient("fake", "https://example.invalid", dimensions=3)
    def documents(*args):
        raise RuntimeError("Embedding batch failed") from failure
    monkeypatch.setattr(client, "embed_documents", documents)
    return client


def test_embedding_wrapped_deadline_remains_request_deadline(monkeypatch):
    client = client_with_failure(monkeypatch, RequestDeadlineExceeded("deadline"))
    with pytest.raises(RequestDeadlineExceeded):
        client.embed_query("question")


def test_embedding_auth_failure_is_not_marked_retryable(monkeypatch):
    class AuthenticationFailure(Exception):
        status_code = 401
    client = client_with_failure(monkeypatch, AuthenticationFailure("unauthorized"))
    with pytest.raises(EmbeddingQueryError) as failure:
        client.embed_query("question")
    assert not failure.value.retryable


def test_embedding_transient_failure_remains_recoverable(monkeypatch):
    client = client_with_failure(monkeypatch, TimeoutError("temporary timeout"))
    with pytest.raises(EmbeddingQueryError) as failure:
        client.embed_query("question")
    assert failure.value.retryable
