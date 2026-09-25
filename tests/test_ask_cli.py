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


def document_result() -> SearchResult:
    return SearchResult(
        id=2,
        source_type="DOCUMENT",
        chunk_type="DOCUMENT_SECTION",
        file_path="docs/order-design.md",
        content="订单创建流程设计。",
        start_line=None,
        end_line=None,
        class_name=None,
        symbol_name=None,
        signature=None,
        title="订单流程",
        score=0.4,
        heading_path=["订单设计", "创建流程"],
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

        def generate(self, messages: list[object]) -> str:
            if "证据充分性审查器" in messages[0].content:  # type: ignore[attr-defined]
                return '{"enough": true, "missing_aspects": [], "reason": "代码证据足够"}'
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
    assert "Sufficiency: enough" in captured.out
    assert "Retries: 0" in captured.out
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
            results = [code_result()] if source_type == "CODE" else [document_result()]
            return SearchExecution(results, SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            self.router = kwargs.get("max_tokens") == 1024
            client_max_tokens.append(kwargs.get("max_tokens"))  # type: ignore[arg-type]

        def generate(self, messages: list[object]) -> str:
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "查询分类器" in system_prompt:
                return "MIXED"
            if "证据充分性审查器" in system_prompt:
                return '{"enough": true, "missing_aspects": [], "reason": "双源证据足够"}'
            return "证据回答 [C1]。"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "帮我看看这个功能目前怎么样"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert client_max_tokens == [1024, 1024, None]
    assert retrieval_calls == [("vector", "CODE"), ("vector", "DOCUMENT")]
    assert "Route: MIXED (llm)" in captured.out
    assert "Sufficiency: enough" in captured.out
    assert "证据回答 [C1]" in captured.out


def test_ask_cli_debug_shows_targeted_retry_trace(monkeypatch, capsys) -> None:
    retrieval_calls: list[tuple[str, str, str | None]] = []
    sufficiency_calls = 0

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
            retrieval_calls.append((strategy, query, source_type))
            if query == "OrderService createOrder 精确代码":
                return SearchExecution([code_result()], SearchTimings())
            if source_type == "DOCUMENT":
                return SearchExecution([document_result()], SearchTimings())
            return SearchExecution([], SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            self.short = kwargs.get("max_tokens") == 1024

        def generate(self, messages: list[object]) -> str:
            nonlocal sufficiency_calls
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "证据充分性审查器" in system_prompt:
                sufficiency_calls += 1
                return '{"enough": true, "missing_aspects": [], "reason": "证据齐全"}'
            if "定向检索查询改写器" in system_prompt:
                return '{"rewritten_query": "OrderService createOrder 精确代码"}'
            return "代码入口见 [C1]，设计依据见 [C2]。"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(
        ["ask", "订单创建如何实现，为什么这样设计？", "--debug"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Route: MIXED (rules)" in captured.out
    assert "Sufficiency: enough" in captured.out
    assert "Retries: 1" in captured.out
    assert '"stop_reason": "sufficient"' in captured.out
    assert '"rewritten_query": "OrderService createOrder 精确代码"' in captured.out
    assert '"selected_chunks"' in captured.out
    assert "代码入口见 [C1]，设计依据见 [C2]" in captured.out
    assert sufficiency_calls == 1
    assert retrieval_calls[-1] == (
        "hybrid",
        "OrderService createOrder 精确代码",
        "CODE",
    )
