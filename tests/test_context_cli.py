from __future__ import annotations

import devcontext.cli as cli_module
from devcontext.models import SearchResult


def code_result() -> SearchResult:
    return SearchResult(
        id=1,
        source_type="CODE",
        chunk_type="METHOD",
        file_path="services/order/OrderService.java",
        content="return createOrder();",
        start_line=81,
        end_line=90,
        class_name="OrderService",
        symbol_name="createOrder",
        signature="public String createOrder()",
        title=None,
        score=0.5,
    )


def test_context_cli_uses_hybrid_retrieval_and_prints_context(
    monkeypatch, capsys
) -> None:
    calls: list[tuple[str, str, int]] = []

    class FakeRetrievalService:
        def __init__(self, settings: object) -> None:
            pass

        def search(self, strategy: str, query: str, top_k: int) -> list[SearchResult]:
            calls.append((strategy, query, top_k))
            return [code_result()]

    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)

    exit_code = cli_module.main(
        ["context", "购票事务是如何实现的？", "--top-k", "3", "--max-chars", "1000"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == [("hybrid", "购票事务是如何实现的？", 3)]
    assert "Query: 购票事务是如何实现的？" in captured.out
    assert "[C1] CODE" in captured.out
    assert "File: services/order/OrderService.java" in captured.out
    assert "Lines: 81-90" in captured.out
    assert captured.err == ""


def test_context_cli_prints_clear_message_for_empty_results(
    monkeypatch, capsys
) -> None:
    class FakeRetrievalService:
        def __init__(self, settings: object) -> None:
            pass

        def search(self, strategy: str, query: str, top_k: int) -> list[SearchResult]:
            return []

    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)

    exit_code = cli_module.main(["context", "no matches"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "Query: no matches\nNo context found.\n"
    assert captured.err == ""
