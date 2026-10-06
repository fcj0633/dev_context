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


def test_teach_is_the_default_answer_mode_and_explain_is_still_reachable() -> None:
    parser = cli_module._parser()

    assert parser.parse_args(["ask", "问题"]).answer_mode == "teach"
    for mode in ("explain", "legacy"):
        assert parser.parse_args(
            ["ask", "问题", "--answer-mode", mode]
        ).answer_mode == mode


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
            "answer_goal": "理解订单关闭的执行链与设计取舍",
            "explanation_strategy": "mixed",
            "sub_questions": [
                {
                    "question": "订单关闭的入口方法在哪里",
                    "purpose": "定位实现",
                    "evidence_description": "订单关闭的触发与入口实现",
                    "preferred_sources": ["CODE"],
                    "retrieval_query": "订单关闭 触发 入口 实现",
                    "importance": "CORE",
                    "temporal_scope": "CURRENT",
                },
                {
                    "question": "订单关闭的设计依据是什么",
                    "purpose": "确认设计原因",
                    "evidence_description": "订单关闭时序与取舍的设计说明",
                    "preferred_sources": ["DOCUMENT"],
                    "retrieval_query": "订单关闭 时序 设计取舍",
                    "importance": "SUPPORTING",
                    "temporal_scope": "CURRENT",
                },
            ],
            "answer_depth": "detailed",
        },
        ensure_ascii=False,
    )


def _evidence_plan_payload() -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "requirements": [
                {
                    "target": "确认订单关闭的触发与入口实现",
                    "success_criteria": "找到当前代码中的触发入口和关闭调用",
                    "priority": "CORE",
                    "temporal_scope": "CURRENT",
                    "source_requirement": "CODE",
                },
                {
                    "target": "确认订单关闭时序的设计依据",
                    "success_criteria": "找到说明关闭时序和设计取舍的当前文档",
                    "priority": "SUPPORTING",
                    "temporal_scope": "CURRENT",
                    "source_requirement": "DOCUMENT",
                },
            ],
        },
        ensure_ascii=False,
    )


def _search_actions_payload() -> str:
    return json.dumps(
        {
            "actions": [
                {
                    "requirement_id": "ER1",
                    "query": "订单关闭 触发 入口 实现",
                    "reason": "定位当前关闭代码",
                },
                {
                    "requirement_id": "ER2",
                    "query": "订单关闭 时序 设计取舍",
                    "reason": "定位当前设计说明",
                },
            ]
        },
        ensure_ascii=False,
    )


def _coverage_payload(*states: str) -> str:
    selected = states or ("SATISFIED", "SATISFIED")
    return json.dumps(
        {
            "statuses": [
                {
                    "requirement_id": f"ER{index}",
                    "state": state,
                    "evidence_ids": [index],
                    "missing_criteria": [] if state == "SATISFIED" else ["尚缺直接证据"],
                    "reason": "证据直接支持该需求" if state == "SATISFIED" else "只有部分证据",
                }
                for index, state in enumerate(selected, start=1)
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
            if "项目证据需求规划器" in system_prompt:
                return _evidence_plan_payload()
            if "查询生成器" in system_prompt:
                return _search_actions_payload()
            if "证据覆盖审查器" in system_prompt:
                return _coverage_payload()
            return "订单关闭分为入口与设计两部分。\n[C1][C2]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "订单关闭是怎么实现的，为什么这样设计？", "--answer-mode", "legacy"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Evidence Plan: llm" in captured.out
    assert "ER1. 确认订单关闭的触发与入口实现 — CODE" in captured.out
    assert "ER2. 确认订单关闭时序的设计依据 — DOCUMENT" in captured.out
    assert "Route: MIXED (rules)" in captured.out
    # Round 0 CODE now searches semantically: neither this question nor anything
    # retrieved yet names a real project symbol, so there is no exact term for a
    # keyword half to match. DOCUMENT is untouched.
    assert retrieval_calls == [("vector", "CODE"), ("vector", "DOCUMENT")]
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
            if "项目证据需求规划器" in system_prompt:
                return _evidence_plan_payload()
            if "查询生成器" in system_prompt:
                return _search_actions_payload()
            if "证据覆盖审查器" in system_prompt:
                return _coverage_payload("SATISFIED", "PARTIAL")
            return "订单关闭分为入口与设计两部分。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "订单关闭怎么实现？", "--debug", "--answer-mode", "legacy"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Requirements: ER1 SATISFIED, ER2 PARTIAL" in captured.out
    trace = json.loads(captured.out.split("Trace:\n", maxsplit=1)[1])
    assert [status["sub_question_id"] for status in trace["final_sufficiency"]["statuses"]] == [
        "ER1",
        "ER2",
    ]
    assert trace["final_sufficiency"]["missing_aspects"][0]["sub_question_id"] == "ER2"


def test_ask_cli_legacy_generation_prompt_excludes_the_plan(
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
            if "项目证据需求规划器" in system_prompt:
                return _evidence_plan_payload()
            if "查询生成器" in system_prompt:
                return _search_actions_payload()
            if "证据覆盖审查器" in system_prompt:
                return _coverage_payload()
            prompts.append(messages[1].content)  # type: ignore[attr-defined]
            return "第一节内容。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    # Pinned to legacy, which is what this was always exercising: it passed
    # before only because legacy happened to be the default. The assertions
    # below are about the legacy generation prompt, not explain's.
    exit_code = cli_module.main(
        ["ask", "订单关闭是怎么实现的，为什么这样设计？", "--answer-mode", "legacy"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert len(prompts) == 1
    assert "Answer Outline:" not in prompts[0]
    assert "Evidence Plan" not in prompts[0]
    assert "只依据以上 Context" in prompts[0]


def test_ask_cli_plan_only_prints_json_without_retrieving(
    monkeypatch, capsys
) -> None:
    retrieval_attempts: list[str] = []
    model_calls: list[str] = []

    class ForbiddenRetrievalService:
        def __init__(self, settings: object) -> None:
            retrieval_attempts.append("constructed")

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def generate(self, messages: list[object]) -> str:
            model_calls.append(messages[0].content)  # type: ignore[attr-defined]
            return _evidence_plan_payload()

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", ForbiddenRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "订单关闭怎么实现？", "--plan-only"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert retrieval_attempts == []
    # Splitting and evidence planning are now one call, not two.
    assert len(model_calls) == 1
    assert "项目证据需求规划器" in model_calls[0]
    plan = json.loads(captured.out)
    assert plan["decision_source"] == "llm"
    assert plan["schema_version"] == 2
    assert [item["id"] for item in plan["requirements"]] == ["ER1", "ER2"]
    assert plan["requirements"][0]["target"] == "确认订单关闭的触发与入口实现"
    assert plan["requirements"][0]["source_requirement"] == "CODE"
    assert plan["requirements"][1]["source_requirement"] == "DOCUMENT"
    assert "answer_depth" not in plan


def test_ask_cli_explain_mode_runs_answer_plan_and_review(
    monkeypatch, capsys
) -> None:
    client_settings: list[dict[str, object]] = []

    class FakeRetrievalService:
        def __init__(self, settings: object) -> None:
            pass

        def search_with_trace(
            self, strategy: str, query: str, top_k: int, *, source_type=None
        ) -> SearchExecution:
            results = [code_result()] if source_type == "CODE" else [document_result()]
            return SearchExecution(results, SearchTimings())

    class FakeDeepSeekClient:
        def __init__(self, **kwargs: object) -> None:
            client_settings.append(kwargs)
            self.model = kwargs.get("model")
            self.reasoning_effort = kwargs.get("reasoning_effort")
            self.last_usage = {"prompt_tokens": 10, "completion_tokens": 5}

        def generate(self, messages: list[object]) -> str:
            system_prompt = messages[0].content  # type: ignore[attr-defined]
            if "项目证据需求规划器" in system_prompt:
                return _evidence_plan_payload()
            if "查询生成器" in system_prompt:
                return _search_actions_payload()
            if "证据覆盖审查器" in system_prompt:
                return _coverage_payload()
            if "回答编排器" in system_prompt:
                return json.dumps({
                    "answer_goal": "理解订单关闭的执行链与设计取舍",
                    "answer_depth": "detailed",
                    "direct_answer": "当前实现由关闭代码与设计约束共同构成。",
                    "summary_citation_labels": ["C1", "C2"],
                    "explanation_strategy": "mixed",
                    "sections": [
                        {"title": "执行主线", "purpose": "解释执行", "key_points": ["入口与关闭"], "evidence_labels": ["C1"], "target_chars": 734},
                        {"title": "设计约束", "purpose": "解释原因", "key_points": ["时序取舍"], "evidence_labels": ["C2"], "target_chars": 733},
                        {"title": "边界", "purpose": "解释边界", "key_points": ["证据范围"], "evidence_labels": ["C1", "C2"], "target_chars": 733},
                    ],
                    "unresolved_gaps": [],
                    "conflicts": [],
                }, ensure_ascii=False)
            if "回答审稿器" in system_prompt:
                return json.dumps({
                    "accepted": False,
                    "issues": [{"issue_type": "POOR_ORDER", "description": "调整顺序"}],
                    "final_answer_with_citations": "审稿后的连贯解释。 [C1][C2]",
                }, ensure_ascii=False)
            return "原始草稿。 [C1][C2]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main([
        "ask", "订单关闭是怎么实现的，为什么这样设计？",
        "--answer-mode", "explain", "--debug",
    ])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "审稿后的连贯解释。" in captured.out
    trace = json.loads(captured.out.split("Trace:\n", maxsplit=1)[1])
    assert trace["answer_plan"]["direct_answer"].startswith("当前实现")
    assert trace["review"]["issues"][0]["issue_type"] == "POOR_ORDER"
    assert any(item.get("reasoning_effort") == "high" for item in client_settings)
    assert any(item.get("max_tokens") == 32768 for item in client_settings)
    assert any(item.get("json_mode") is True for item in client_settings)


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
            if "项目证据需求规划器" in system_prompt:
                return "not a json plan"
            if "证据覆盖审查器" in system_prompt:
                return json.dumps({"statuses": [{
                    "requirement_id": "ER1", "state": "SATISFIED",
                    "evidence_ids": [1], "missing_criteria": [], "reason": "足够",
                }]}, ensure_ascii=False)
            return "回退回答。\n[C1]"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret")
    monkeypatch.setattr(cli_module, "RetrievalService", FakeRetrievalService)
    monkeypatch.setattr(cli_module, "DeepSeekLLMClient", FakeDeepSeekClient)

    exit_code = cli_module.main(["ask", "创建订单的代码在哪里？", "--answer-mode", "legacy"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Evidence Plan: fallback" in captured.out
    assert retrieval_calls[0][0] == "hybrid"
    assert "创建订单的代码在哪里" in retrieval_calls[0][1]
    assert "回退回答。" in captured.out


def test_evaluate_answers_parses_its_arguments() -> None:
    parser = cli_module._parser()

    defaults = parser.parse_args(["evaluate-answers"])
    assert defaults.cases is None
    assert defaults.only is None
    assert defaults.limit is None
    assert defaults.resume is False
    assert defaults.judge_model is None

    explicit = parser.parse_args(
        [
            "evaluate-answers",
            "--only", "flow-01,locate-01",
            "--limit", "2",
            "--resume",
            "--judge-model", "deepseek-v4-pro",
        ]
    )
    assert explicit.only == "flow-01,locate-01"
    assert explicit.limit == 2
    assert explicit.resume is True
    assert explicit.judge_model == "deepseek-v4-pro"


def test_evaluate_answers_forces_the_case_depth_and_report_path(
    monkeypatch, tmp_path, capsys
) -> None:
    captured: dict[str, object] = {}
    workflow_calls: list[tuple[tuple, dict]] = []

    def fake_workflow(*args, **kwargs):
        workflow_calls.append((args, kwargs))
        return "workflow"

    def fake_runner(**kwargs):
        captured.update(kwargs)
        kwargs["workflow_factory"]("explain", "detailed")
        return {
            "summary": {
                "judge_winners": {"V2": 18},
                "arms": {},
                "acceptance": [],
                "human_review_queue": [],
            },
            "records": [],
        }

    monkeypatch.setattr(cli_module, "_planned_workflow", fake_workflow)
    monkeypatch.setattr(cli_module, "run_answer_quality_evaluation", fake_runner)
    output = tmp_path / "report.json"

    code = cli_module.main(
        [
            "evaluate-answers",
            "--only", "flow-01",
            "--judge-model", "deepseek-v4-pro",
            "--output", str(output),
        ]
    )

    assert code == 0
    assert captured["only"] == ["flow-01"]
    # the depth comes from the dataset case, never from the planner
    assert len(workflow_calls) == 1
    args, _ = workflow_calls[0]
    assert args[3] == "explain"  # answer_mode
    assert args[4] is None  # no explicit context override; EvidencePlan complexity governs
    assert len(args) == 5  # case depth is historical metadata, not a runtime control
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["judge_winners"] == {
        "V2": 18
    }
    assert json.loads(capsys.readouterr().out)["judge_winners"] == {"V2": 18}
