from devcontext.evaluation.runner import recall_at, reciprocal_rank
from devcontext.models import SearchResult


def result(identifier: int, path: str, symbol: str) -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type="CODE",
        chunk_type="METHOD",
        file_path=path,
        content="content",
        start_line=1,
        end_line=2,
        class_name="Service",
        symbol_name=symbol,
        signature=None,
        title=None,
        score=1.0,
    )


def test_metrics_match_multiple_targets() -> None:
    results = [result(1, "order/A.java", "first"), result(2, "ticket/B.java", "second")]
    relevant = [
        {"source_type": "CODE", "symbol": "second"},
        {"source_type": "CODE", "path_contains": "order/"},
    ]

    assert recall_at(results, relevant, 1) == 0.5
    assert recall_at(results, relevant, 2) == 1.0
    assert reciprocal_rank(results, relevant) == 1.0


def test_metrics_accept_an_explicit_alternative_document_path() -> None:
    results = [result(1, "docs/模块3-技术专题.md", "none")]
    relevant = [
        {
            "source_type": "CODE",
            "path_contains_any": ["D3-设计分析.md", "模块3-技术专题.md"],
        }
    ]

    assert recall_at(results, relevant, 1) == 1.0
