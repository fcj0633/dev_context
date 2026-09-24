from __future__ import annotations

import devcontext.cli as cli_module
from devcontext.models import SearchExecution, SearchResult, SearchTimings


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


def test_ask_cli_runs_grounded_pipeline_and_prints_real_sources(
    monkeypatch, capsys
) -> None:
    retrieval_calls: list[tuple[str, str, int, str | None]] = []
    client_settings: list[dict[str, object]] = []

    class FakeRetrievalService:
        def __init__(self, settings: object) -> None:
            pass

        def search_with_trace(
            self,
            strategy: str,
            query: str,
            top_k: int,
            *,
            source_type: str | None = None,
        ) -> SearchExecution:
            retrieval_calls.append((strategy, query, top_k, source_type))
            return SearchExecution([code_result()], SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            client_settings.append(kwargs)

        def generate(self, messages: object) -> str:
            return "订单由检索到的方法创建 [C1]；不要采用模型声称的 fake/path。"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(
        ["ask", "创建订单的代码在哪里？", "--top-k", "3", "--max-chars", "1000"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert retrieval_calls == [("hybrid", "创建订单的代码在哪里？", 3, "CODE")]
    assert client_settings[0]["model"] == "deepseek-flash"
    assert "Question:\n创建订单的代码在哪里？" in captured.out
    assert "Route: CODE (rules)" in captured.out
    assert "订单由检索到的方法创建 [C1]" in captured.out
    sources = captured.out.split("Sources:\n", maxsplit=1)[1]
    assert sources == (
        "[C1] services/order/OrderService.java:81-90 — OrderService#createOrder\n"
    )
    assert "fake/path" not in sources
    assert captured.err == ""


def test_ask_cli_empty_context_does_not_construct_llm_client(
    monkeypatch, capsys
) -> None:
    class FakeRetrievalService:
        def __init__(self, settings: object) -> None:
            pass

        def search_with_trace(
            self,
            strategy: str,
            query: str,
            top_k: int,
            *,
            source_type: str | None = None,
        ) -> SearchExecution:
            return SearchExecution([], SearchTimings())

    class ForbiddenLLMClient:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("LLM client must not be created for empty context")

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", ForbiddenLLMClient)

    exit_code = cli_module.main(["ask", "没有结果的代码在哪里"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "当前没有检索到足够的项目上下文" in captured.out
    assert "Sources:\n(none)" in captured.out
    assert captured.err == ""


def test_ask_cli_invalid_citation_fails_without_printing_answer(
    monkeypatch, capsys
) -> None:
    class FakeRetrievalService:
        def __init__(self, settings: object) -> None:
            pass

        def search_with_trace(
            self,
            strategy: str,
            query: str,
            top_k: int,
            *,
            source_type: str | None = None,
        ) -> SearchExecution:
            return SearchExecution([code_result()], SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def generate(self, messages: object) -> str:
            return "这是一份不可信答案 [C9]。"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "代码在哪里"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert "[C9]" in captured.err
    assert "不可信答案" not in captured.out


def test_ask_cli_uses_llm_fallback_for_ambiguous_query(monkeypatch, capsys) -> None:
    retrieval_calls: list[tuple[str, str | None]] = []
    client_max_tokens: list[int | None] = []

    class FakeRetrievalService:
        def __init__(self, settings: object) -> None:
            pass

        def search_with_trace(
            self,
            strategy: str,
            query: str,
            top_k: int,
            *,
            source_type: str | None = None,
        ) -> SearchExecution:
            retrieval_calls.append((strategy, source_type))
            return SearchExecution([code_result()], SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            self.router = kwargs.get("max_tokens") == 1024
            client_max_tokens.append(kwargs.get("max_tokens"))  # type: ignore[arg-type]

        def generate(self, messages: object) -> str:
            return "MIXED" if self.router else "证据回答 [C1]。"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "帮我看看这个功能目前怎么样"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert client_max_tokens == [1024, None]
    assert retrieval_calls == [("vector", "CODE"), ("vector", "DOCUMENT")]
    assert "Route: MIXED (llm)" in captured.out
    assert "证据回答 [C1]" in captured.out
