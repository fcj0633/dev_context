from __future__ import annotations

import json

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


def test_ask_cli_runs_grounded_pipeline_without_printing_sources(
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
            return "订单由检索到的方法创建。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(
        [
            "ask",
            "创建订单的代码在哪里？",
            "--no-plan",
            "--top-k",
            "3",
            "--max-chars",
            "1000",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert retrieval_calls == [("hybrid", "创建订单的代码在哪里？", 3, "CODE")]
    assert client_settings[0]["model"] == "deepseek-flash"
    assert 8192 in [settings.get("max_tokens") for settings in client_settings]
    assert "Question:\n创建订单的代码在哪里？" in captured.out
    assert "Route: CODE (rules)" in captured.out
    assert "Sufficiency: enough" in captured.out
    assert "Retries: 0" in captured.out
    assert "订单由检索到的方法创建。" in captured.out
    assert "[C1]" not in captured.out
    assert "Sources:" not in captured.out
    assert captured.err == ""


def test_ask_cli_debug_prints_real_sources_and_citations(
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

        def generate(self, messages: list[object]) -> str:
            if "证据充分性审查器" in messages[0].content:  # type: ignore[attr-defined]
                return '{"enough": true, "missing_aspects": [], "reason": "足够"}'
            return "订单由检索到的方法创建。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(
        ["ask", "创建订单的代码在哪里？", "--no-plan", "--debug"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    sources = captured.out.split("Sources:\n", maxsplit=1)[1]
    assert sources.startswith(
        "[C1] services/order/OrderService.java:81-90 — OrderService#createOrder"
    )
    assert "fake/path" not in sources
    trace = json.loads(captured.out.split("Trace:\n", maxsplit=1)[1])
    assert trace["citations"] == {
        "used_labels": ["C1"],
        "invalid_labels": [],
        "zero_valid": False,
        "sources": [
            "[C1] services/order/OrderService.java:81-90 — OrderService#createOrder"
        ],
    }
    assert trace["sections"] == {
        "count": 0,
        "lengths": [],
        "max_chars": 0,
        "over_budget": 0,
    }


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

    exit_code = cli_module.main(["ask", "没有结果的代码在哪里", "--no-plan"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "当前没有检索到足够的项目上下文" in captured.out
    assert "Sources:" not in captured.out
    assert captured.err == ""


def test_ask_cli_drops_invalid_citations_and_still_answers(
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

        def generate(self, messages: list[object]) -> str:
            if "证据充分性审查器" in messages[0].content:  # type: ignore[attr-defined]
                return '{"enough": true, "missing_aspects": [], "reason": "足够"}'
            return "这是一份带有错误标签的答案。\n[C9]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(
        ["ask", "代码在哪里", "--no-plan", "--debug"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "这是一份带有错误标签的答案。" in captured.out
    assert "[C9]" not in captured.out
    assert "Sources:\n(none)" in captured.out
    trace = json.loads(captured.out.split("Trace:\n", maxsplit=1)[1])
    assert trace["citations"]["used_labels"] == []
    assert trace["citations"]["invalid_labels"] == ["C9"]
    assert trace["citations"]["zero_valid"] is True


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
            client_max_tokens.append(kwargs.get("max_tokens"))  # type: ignore[arg-type]

        def generate(self, messages: list[object]) -> str:
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "查询分类器" in system_prompt:
                return "MIXED"
            if "证据充分性审查器" in system_prompt:
                return '{"enough": true, "missing_aspects": [], "reason": "双源证据足够"}'
            return "证据回答。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "帮我看看这个功能目前怎么样", "--no-plan"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert client_max_tokens == [1024, 1024, 8192]
    assert retrieval_calls == [("vector", "CODE"), ("vector", "DOCUMENT")]
    assert "Route: MIXED (llm)" in captured.out
    assert "Sufficiency: enough" in captured.out
    assert "证据回答。" in captured.out


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
            return "代码入口见 OrderService#createOrder。\n[C1][C2]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(
        ["ask", "订单创建如何实现，为什么这样设计？", "--no-plan", "--debug"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Route: MIXED (rules)" in captured.out
    assert "Sufficiency: enough" in captured.out
    assert "Retries: 1" in captured.out
    assert '"stop_reason": "sufficient"' in captured.out
    assert '"rewritten_query": "OrderService createOrder 精确代码"' in captured.out
    assert '"selected_chunks"' in captured.out
    assert "代码入口见 OrderService#createOrder。" in captured.out
    assert sufficiency_calls == 1
    assert retrieval_calls[-1] == (
        "hybrid",
        "OrderService createOrder 精确代码",
        "CODE",
    )


def _planner_payload() -> str:
    return json.dumps(
        {
            "intent_summary": "用户想了解订单关闭的实现与设计",
            "sub_questions": [
                {"question": "订单关闭的入口方法在哪里", "purpose": "定位实现"},
                {"question": "订单关闭的设计依据是什么", "purpose": "确认设计原因"},
            ],
            "answer_depth": "detailed",
        },
        ensure_ascii=False,
    )


def _evidence_payload() -> str:
    return json.dumps(
        {
            "requirements": [
                {
                    "description": "订单关闭的触发与入口实现",
                    "preferred_sources": ["CODE"],
                },
                {
                    "description": "订单关闭时序与取舍的设计说明",
                    "preferred_sources": ["DOCUMENT"],
                },
            ]
        },
        ensure_ascii=False,
    )


def _requirement_statuses_payload(*satisfied: bool) -> str:
    return json.dumps(
        {
            "statuses": [
                {"satisfied": flag, "reason": "证据直接支持该需求"}
                for flag in (satisfied or (True, True))
            ]
        },
        ensure_ascii=False,
    )


def test_ask_cli_planned_path_fans_out_over_sub_questions(
    monkeypatch, capsys
) -> None:
    retrieval_calls: list[tuple[str, str | None]] = []

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
            pass

        def generate(self, messages: list[object]) -> str:
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "问题规划器" in system_prompt:
                return _planner_payload()
            if "证据规划器" in system_prompt:
                return _evidence_payload()
            if "证据需求审查器" in system_prompt:
                return _requirement_statuses_payload()
            if "证据充分性审查器" in system_prompt:
                return '{"enough": true, "missing_aspects": [], "reason": "双源足够"}'
            return "订单关闭分为入口与设计两部分。\n[C1][C2]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "订单关闭是怎么实现的，为什么这样设计？"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Plan: llm" in captured.out
    assert "Intent: 用户想了解订单关闭的实现与设计" in captured.out
    assert "SQ1. 订单关闭的入口方法在哪里 — 定位实现" in captured.out
    assert "SQ2. 订单关闭的设计依据是什么 — 确认设计原因" in captured.out
    assert "Route: MIXED (rules)" in captured.out
    assert retrieval_calls == [("hybrid", "CODE"), ("vector", "DOCUMENT")]
    assert "订单关闭分为入口与设计两部分。" in captured.out
    assert "Sources:" not in captured.out


def test_ask_cli_debug_prints_per_requirement_status(monkeypatch, capsys) -> None:
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
            results = [code_result()] if source_type == "CODE" else [document_result()]
            return SearchExecution(results, SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def generate(self, messages: list[object]) -> str:
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "问题规划器" in system_prompt:
                return _planner_payload()
            if "证据规划器" in system_prompt:
                return _evidence_payload()
            if "证据需求审查器" in system_prompt:
                return _requirement_statuses_payload(True, False)
            return "订单关闭分为入口与设计两部分。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "订单关闭怎么实现？", "--debug"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Requirements: ER1 ok, ER2 missing" in captured.out
    trace = json.loads(captured.out.split("Trace:\n", maxsplit=1)[1])
    assert [status["requirement_id"] for status in trace["final_sufficiency"]["statuses"]] == [
        "ER1",
        "ER2",
    ]
    assert trace["final_sufficiency"]["missing_aspects"][0]["requirement_id"] == "ER2"


def test_ask_cli_planned_path_asks_for_a_sectioned_answer(
    monkeypatch, capsys
) -> None:
    prompts: list[str] = []

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
            results = [code_result()] if source_type == "CODE" else [document_result()]
            return SearchExecution(results, SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def generate(self, messages: list[object]) -> str:
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "问题规划器" in system_prompt:
                return _planner_payload()
            if "证据规划器" in system_prompt:
                return _evidence_payload()
            if "证据需求审查器" in system_prompt:
                return _requirement_statuses_payload()
            if "证据充分性审查器" in system_prompt:
                return '{"enough": true, "missing_aspects": [], "reason": "足够"}'
            prompts.append(messages[1].content)  # type: ignore[attr-defined]
            return "第一节内容。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "订单关闭是怎么实现的，为什么这样设计？"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert len(prompts) == 1
    assert "Answer Outline:" in prompts[0]
    assert "1. 订单关闭的入口方法在哪里" in prompts[0]
    assert "150–350 字" in prompts[0]


def test_ask_cli_plan_only_prints_json_without_retrieving(
    monkeypatch, capsys
) -> None:
    retrieval_attempts: list[str] = []

    class ForbiddenRetrievalService:
        def __init__(self, settings: object) -> None:
            retrieval_attempts.append("constructed")

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def generate(self, messages: list[object]) -> str:
            if "问题规划器" in messages[0].content:  # type: ignore[attr-defined]
                return _planner_payload()
            return _evidence_payload()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", ForbiddenRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "订单关闭怎么实现？", "--plan-only"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert retrieval_attempts == []
    plan = json.loads(captured.out)
    assert plan["decision_source"] == "llm"
    assert plan["sub_questions"][0]["id"] == "SQ1"
    assert plan["sub_questions"][0]["purpose"] == "定位实现"
    evidence = plan["evidence_plan"]
    assert evidence["decision_source"] == "llm"
    assert [item["id"] for item in evidence["requirements"]] == ["ER1", "ER2"]
    assert [item["sub_question_id"] for item in evidence["requirements"]] == [
        "SQ1",
        "SQ2",
    ]
    assert evidence["requirements"][0]["preferred_sources"] == ["CODE"]
    assert evidence["requirements"][1]["preferred_sources"] == ["DOCUMENT"]


def test_ask_cli_planning_failure_falls_back_to_legacy_single_query(
    monkeypatch, capsys
) -> None:
    retrieval_calls: list[tuple[str, str]] = []

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
            retrieval_calls.append((strategy, query))
            return SearchExecution([code_result()], SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def generate(self, messages: list[object]) -> str:
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "问题规划器" in system_prompt:
                return "not a json plan"
            if "证据充分性审查器" in system_prompt:
                return '{"enough": true, "missing_aspects": [], "reason": "足够"}'
            return "回退回答。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "创建订单的代码在哪里？"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Plan: fallback" in captured.out
    assert retrieval_calls == [("hybrid", "创建订单的代码在哪里？")]
    assert "回退回答。" in captured.out
