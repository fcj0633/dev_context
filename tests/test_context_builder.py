from __future__ import annotations

import pytest

from devcontext.context.builder import ContextBuilder, TRUNCATION_MARKER
from devcontext.models import SearchResult


def result(
    identifier: int,
    *,
    source_type: str = "CODE",
    content: str = "context body",
    score: float = 0.5,
    file_path: str | None = None,
) -> SearchResult:
    is_code = source_type == "CODE"
    return SearchResult(
        id=identifier,
        source_type=source_type,
        chunk_type="METHOD" if is_code else "DOCUMENT_SECTION",
        file_path=file_path or ("OrderService.java" if is_code else "order-design.md"),
        content=content,
        start_line=81 if is_code else 20,
        end_line=120 if is_code else 30,
        class_name="OrderService" if is_code else None,
        symbol_name="createOrder" if is_code else None,
        signature="public String createOrder()" if is_code else None,
        title=None if is_code else "4.2 订单状态机",
        score=score,
        annotations=["@Transactional"] if is_code else [],
        heading_path=[] if is_code else ["四、订单模块", "4.2 订单状态机"],
    )


def test_duplicate_chunk_keeps_first_result_only() -> None:
    first = result(1, content="first", score=0.9)
    duplicate = result(1, content="duplicate", score=0.1)

    bundle = ContextBuilder().build("query", [first, duplicate])

    assert len(bundle.items) == 1
    assert bundle.items[0].content == "first"
    assert bundle.items[0].score == 0.9
    assert bundle.items[0].retrieval_rank == 1
    assert bundle.truncated is False


def test_java_citation_and_rendering() -> None:
    bundle = ContextBuilder().build("query", [result(1)])
    citation = bundle.items[0].citation

    assert citation.label == "C1"
    assert citation.source_type == "CODE"
    assert citation.file_path == "OrderService.java"
    assert citation.class_name == "OrderService"
    assert citation.symbol_name == "createOrder"
    assert citation.signature == "public String createOrder()"
    assert citation.start_line == 81
    assert citation.end_line == 120
    assert "[C1] CODE" in bundle.rendered_text
    assert "Symbol: OrderService#createOrder" in bundle.rendered_text
    assert "Lines: 81-120" in bundle.rendered_text
    assert "Score: 0.500000" in bundle.rendered_text


def test_markdown_citation_and_rendering() -> None:
    bundle = ContextBuilder().build(
        "query", [result(2, source_type="DOCUMENT")]
    )
    citation = bundle.items[0].citation

    assert citation.label == "C1"
    assert citation.source_type == "DOCUMENT"
    assert citation.file_path == "order-design.md"
    assert citation.heading_path == ["四、订单模块", "4.2 订单状态机"]
    assert "[C1] DOCUMENT" in bundle.rendered_text
    assert "Heading: 四、订单模块 > 4.2 订单状态机" in bundle.rendered_text


def test_citation_numbers_and_rendered_text_are_stable() -> None:
    results = [result(1), result(2, source_type="DOCUMENT"), result(3)]
    builder = ContextBuilder()

    first = builder.build("query", results)
    second = builder.build("query", results)

    assert [item.citation.label for item in first.items] == ["C1", "C2", "C3"]
    assert first.to_dict() == second.to_dict()


def test_budget_truncates_last_item_and_counts_full_rendered_text() -> None:
    long_result = result(1, content="abcdefghij" * 50)
    complete = ContextBuilder(max_chars=10_000).build("query", [long_result])
    constrained = ContextBuilder(max_chars=complete.total_chars - 20).build(
        "query", [long_result]
    )

    assert len(constrained.items) == 1
    assert constrained.items[0].truncated is True
    assert constrained.items[0].content.endswith(TRUNCATION_MARKER)
    assert constrained.truncated is True
    assert constrained.total_chars == len(constrained.rendered_text)
    assert constrained.total_chars <= constrained.max_chars


def test_item_is_not_added_when_citation_header_does_not_fit() -> None:
    search_result = result(1, content="body")
    complete = ContextBuilder(max_chars=10_000).build("query", [search_result])
    header = complete.rendered_text.split("\n\n", maxsplit=1)[0]

    constrained = ContextBuilder(max_chars=len(header) - 1).build(
        "query", [search_result]
    )

    assert constrained.items == []
    assert constrained.rendered_text == ""
    assert constrained.total_chars == 0
    assert constrained.truncated is True


def test_mixed_sources_are_prioritized_when_both_anchors_fit() -> None:
    first_document = result(1, source_type="DOCUMENT", content="doc one")
    second_document = result(2, source_type="DOCUMENT", content="doc two")
    code = result(3, source_type="CODE", content="code")

    bundle = ContextBuilder().build(
        "query", [first_document, second_document, code]
    )

    assert [item.chunk_id for item in bundle.items] == [1, 3, 2]
    assert [item.citation.source_type for item in bundle.items[:2]] == [
        "DOCUMENT",
        "CODE",
    ]
    assert [item.retrieval_rank for item in bundle.items] == [1, 3, 2]


def test_mixed_sources_fall_back_to_rank_order_when_anchors_do_not_fit() -> None:
    first_document = result(1, source_type="DOCUMENT", content="d" * 200)
    second_document = result(2, source_type="DOCUMENT", content="short")
    code = result(3, source_type="CODE", content="c" * 200)
    full_first = ContextBuilder(max_chars=10_000).build("query", [first_document])

    bundle = ContextBuilder(max_chars=full_first.total_chars).build(
        "query", [first_document, second_document, code]
    )

    assert [item.chunk_id for item in bundle.items] == [1]
    assert bundle.items[0].retrieval_rank == 1
    assert bundle.truncated is True


def test_single_source_input_does_not_invent_another_source() -> None:
    bundle = ContextBuilder().build(
        "query",
        [result(1, source_type="DOCUMENT"), result(2, source_type="DOCUMENT")],
    )

    assert {item.citation.source_type for item in bundle.items} == {"DOCUMENT"}


def test_empty_results_return_empty_bundle() -> None:
    bundle = ContextBuilder().build("query", [])

    assert bundle.query == "query"
    assert bundle.items == []
    assert bundle.rendered_text == ""
    assert bundle.total_chars == 0
    assert bundle.max_chars == 6000
    assert bundle.truncated is False


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_rejected(query: str) -> None:
    with pytest.raises(ValueError, match="query must not be empty"):
        ContextBuilder().build(query, [])


@pytest.mark.parametrize("max_chars", [0, -1])
def test_non_positive_budget_is_rejected(max_chars: int) -> None:
    with pytest.raises(ValueError, match="max_chars must be greater than 0"):
        ContextBuilder(max_chars=max_chars)
