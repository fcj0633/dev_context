from devcontext.models import Chunk


def test_embedding_text_excludes_metadata_noise() -> None:
    chunk = Chunk(
        repository="my12306",
        source_type="CODE",
        chunk_type="METHOD",
        file_path="absolute-looking/path.java",
        signature="public void purchaseTicket()",
        content="return;",
        javadoc="Purchase one ticket.",
    )

    text = chunk.embedding_text()

    assert "purchaseTicket" in text
    assert "Purchase one ticket" in text
    assert "absolute-looking" not in text
    assert "repository" not in text


def test_content_hash_is_stable() -> None:
    values = {
        "repository": "my12306",
        "source_type": "DOCUMENT",
        "chunk_type": "DOCUMENT_SECTION",
        "file_path": "design.md",
        "content": "same content",
    }
    assert Chunk(**values).content_hash == Chunk(**values).content_hash
