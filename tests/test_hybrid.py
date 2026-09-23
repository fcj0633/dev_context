from devcontext.models import SearchResult
from devcontext.retrieval.hybrid import reciprocal_rank_fusion


def result(identifier: int, score: float = 1.0) -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type="CODE",
        chunk_type="METHOD",
        file_path=f"{identifier}.java",
        content="content",
        start_line=1,
        end_line=1,
        class_name="Example",
        symbol_name=f"method{identifier}",
        signature=None,
        title=None,
        score=score,
    )


def test_rrf_merges_duplicates_and_uses_stable_order() -> None:
    fused = reciprocal_rank_fusion(
        [[result(1), result(2)], [result(2), result(1), result(3)]],
        k=60,
        top_k=3,
    )

    assert [item.id for item in fused] == [1, 2, 3]
    assert len({item.id for item in fused}) == 3
    assert fused[0].score == fused[1].score
