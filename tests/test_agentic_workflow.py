from __future__ import annotations

import pytest

from devcontext.agentic import (
    AgenticRetrievalWorkflow,
    MissingAspect,
    QueryRewriteError,
    RewriteResult,
    SufficiencyResult,
)
from devcontext.agentic.workflow import _merge_results
from devcontext.answer import AnswerGenerator, InvalidCitationError
from devcontext.context import ContextBuilder
from devcontext.models import SearchResult
from devcontext.routing import DecisionSource, QueryType, RouteDecision


def result(identifier: int, source_type: str) -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type=source_type,
        chunk_type="METHOD" if source_type == "CODE" else "DOCUMENT_SECTION",
        file_path="Service.java" if source_type == "CODE" else "design.md",
        content=f"evidence {identifier}",
        start_line=10 if source_type == "CODE" else None,
        end_line=20 if source_type == "CODE" else None,
        class_name="Service" if source_type == "CODE" else None,
        symbol_name="run" if source_type == "CODE" else None,
        signature="void run()" if source_type == "CODE" else None,
        title="设计" if source_type == "DOCUMENT" else None,
        score=1.0,
        heading_path=[] if source_type == "CODE" else ["设计", "流程"],
    )


def decision(query_type: QueryType) -> RouteDecision:
    return RouteDecision(query_type, DecisionSource.RULES, "test")


class FakeRouter:
    def __init__(self, route: RouteDecision) -> None:
        self.route_decision = route
        self.calls: list[str] = []

    def route(self, query: str) -> RouteDecision:
        self.calls.append(query)
        return self.route_decision


class FakePolicy:
    def __init__(self, responses: list[list[SearchResult]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, QueryType, int]] = []

    def search(
        self, query: str, route: RouteDecision, top_k: int
    ) -> list[SearchResult]:
        self.calls.append((query, route.query_type, top_k))
        return self.responses.pop(0)


class SourceAwareChecker:
    def __init__(self, route: QueryType) -> None:
        self.route = route
        self.calls = 0

    def check(self, query: str, route: RouteDecision, bundle: object) -> SufficiencyResult:
        self.calls += 1
        source_types = {
            item.citation.source_type for item in bundle.items  # type: ignore[attr-defined]
        }
        required = {
            QueryType.CODE: {"CODE"},
            QueryType.DOC: {"DOCUMENT"},
            QueryType.MIXED: {"CODE", "DOCUMENT"},
        }[self.route]
        missing = required - source_types
        if not missing:
            return SufficiencyResult(True, (), "证据足够", "llm")
        aspects = tuple(
            MissingAspect(source, f"缺少 {source}")
            for source in ("CODE", "DOCUMENT")
            if source in missing
        )
        return SufficiencyResult(False, aspects, "证据不足", "rules")


class StaticRewriter:
    def __init__(self, rewrites: list[tuple[str, QueryType]]) -> None:
        self.rewrites = list(rewrites)
        self.calls = 0

    def rewrite(
        self,
        original_query: str,
        route: RouteDecision,
        sufficiency: SufficiencyResult,
        bundle: object,
    ) -> RewriteResult:
        self.calls += 1
        query, query_type = self.rewrites.pop(0)
        return RewriteResult(
            original_query,
            query,
            query_type,
            sufficiency.missing_aspects,
        )


class FakeAnswerClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[object] = []

    def generate(self, messages: object) -> str:
        self.calls.append(messages)
        return self.answer


def workflow(
    route_type: QueryType,
    policy: FakePolicy,
    checker: object,
    rewriter: object,
    answer_client: FakeAnswerClient,
    *,
    max_retries: int = 2,
) -> tuple[AgenticRetrievalWorkflow, FakeRouter]:
    router = FakeRouter(decision(route_type))
    value = AgenticRetrievalWorkflow(
        router=router,  # type: ignore[arg-type]
        retrieval_policy=policy,  # type: ignore[arg-type]
        context_builder=ContextBuilder(max_chars=3000),
        sufficiency_checker=checker,  # type: ignore[arg-type]
        query_rewriter=rewriter,  # type: ignore[arg-type]
        answer_generator_factory=lambda: AnswerGenerator(answer_client),
        max_retries=max_retries,
    )
    return value, router


def test_sufficient_first_round_does_not_rewrite_or_retry() -> None:
    policy = FakePolicy([[result(1, "CODE")]])
    checker = SourceAwareChecker(QueryType.CODE)
    rewriter = StaticRewriter([])
    answer_client = FakeAnswerClient("实现见 [C1]。")
    runner, router = workflow(
        QueryType.CODE, policy, checker, rewriter, answer_client
    )

    output = runner.run("实现在哪里？", 5)

    assert router.calls == ["实现在哪里？"]
    assert len(policy.calls) == 1
    assert rewriter.calls == 0
    assert output.trace.retry_count == 0
    assert output.trace.stop_reason == "sufficient"
    assert output.answer_result.used_citations == ["C1"]
    assert len(output.trace.rounds) == 1


def test_missing_mixed_side_rewrites_retrieves_and_stops_when_enough() -> None:
    document = result(10, "DOCUMENT")
    code = result(1, "CODE")
    policy = FakePolicy([[document], [code, code]])
    checker = SourceAwareChecker(QueryType.MIXED)
    rewriter = StaticRewriter([("Service run 直接代码", QueryType.CODE)])
    answer_client = FakeAnswerClient("实现见 [C1]，设计见 [C2]。")
    runner, _ = workflow(
        QueryType.MIXED, policy, checker, rewriter, answer_client
    )

    output = runner.run("实现与设计是什么？", 5)

    assert policy.calls == [
        ("实现与设计是什么？", QueryType.MIXED, 5),
        ("Service run 直接代码", QueryType.CODE, 5),
    ]
    assert checker.calls == 2
    assert rewriter.calls == 1
    assert output.trace.retry_count == 1
    assert output.trace.stop_reason == "sufficient"
    assert [item.chunk_id for item in output.context_bundle.items] == [1, 10]
    assert [item.citation.source_type for item in output.context_bundle.items] == [
        "CODE",
        "DOCUMENT",
    ]
    assert output.trace.rounds[0].rewrite is not None
    assert output.trace.rounds[1].rewrite is None
    assert output.answer_result.used_citations == ["C1", "C2"]
    assert output.context_bundle.total_chars <= output.context_bundle.max_chars


def test_persistent_insufficiency_stops_after_two_retries_and_answers_partially() -> None:
    policy = FakePolicy(
        [[result(1, "CODE")], [result(2, "CODE")], [result(3, "CODE")]]
    )

    class AlwaysInsufficient:
        def check(self, query: str, route: RouteDecision, bundle: object) -> SufficiencyResult:
            return SufficiencyResult(
                False,
                (MissingAspect("CODE", "仍缺少目标方法"),),
                "不充分",
                "llm",
            )

    rewriter = StaticRewriter(
        [("目标方法 精确查询 1", QueryType.CODE), ("目标方法 精确查询 2", QueryType.CODE)]
    )
    answer_client = FakeAnswerClient("现有证据只能确认这一部分 [C1]，目标方法仍不能确认。")
    runner, _ = workflow(
        QueryType.CODE, policy, AlwaysInsufficient(), rewriter, answer_client
    )

    output = runner.run("目标方法在哪里？", 5)

    assert len(policy.calls) == 3
    assert rewriter.calls == 2
    assert output.trace.retry_count == 2
    assert output.trace.stop_reason == "retry_limit"
    assert len(output.trace.rounds) == 3
    assert [item.chunk_id for item in output.context_bundle.items] == [3, 2, 1]
    messages = answer_client.calls[0]
    assert "Known Evidence Gaps" in messages[1].content  # type: ignore[index]
    assert "仍缺少目标方法" in messages[1].content  # type: ignore[index]


def test_rewrite_failure_stops_and_uses_partial_answer() -> None:
    policy = FakePolicy([[result(1, "CODE")]])

    class Insufficient:
        def check(self, query: str, route: RouteDecision, bundle: object) -> SufficiencyResult:
            return SufficiencyResult(
                False, (MissingAspect("CODE", "缺少直接证据"),), "不足", "llm"
            )

    class BrokenRewriter:
        def rewrite(self, *args: object) -> RewriteResult:
            raise QueryRewriteError("query rewrite returned invalid JSON")

    answer_client = FakeAnswerClient("仅能确认当前代码 [C1]。")
    runner, _ = workflow(
        QueryType.CODE, policy, Insufficient(), BrokenRewriter(), answer_client
    )

    output = runner.run("问题", 5)

    assert output.trace.stop_reason == "rewrite_failed"
    assert output.trace.retry_count == 0
    assert output.trace.rounds[0].rewrite_error == (
        "query rewrite returned invalid JSON"
    )
    assert len(answer_client.calls) == 1


def test_empty_context_never_constructs_answer_generator() -> None:
    policy = FakePolicy([[]])
    checker = SourceAwareChecker(QueryType.CODE)

    class BrokenRewriter:
        def rewrite(self, *args: object) -> RewriteResult:
            raise QueryRewriteError("unavailable")

    constructed = False

    def forbidden_factory() -> AnswerGenerator:
        nonlocal constructed
        constructed = True
        raise AssertionError("answer generator must not be constructed")

    runner = AgenticRetrievalWorkflow(
        router=FakeRouter(decision(QueryType.CODE)),  # type: ignore[arg-type]
        retrieval_policy=policy,  # type: ignore[arg-type]
        context_builder=ContextBuilder(),
        sufficiency_checker=checker,  # type: ignore[arg-type]
        query_rewriter=BrokenRewriter(),  # type: ignore[arg-type]
        answer_generator_factory=forbidden_factory,
    )

    output = runner.run("没有结果的代码在哪里？", 5)

    assert constructed is False
    assert output.trace.stop_reason == "empty_context"
    assert output.answer_result.used_citations == []
    assert "缺少" in output.answer_result.answer


def test_invalid_final_citation_still_fails_closed() -> None:
    runner, _ = workflow(
        QueryType.CODE,
        FakePolicy([[result(1, "CODE")]]),
        SourceAwareChecker(QueryType.CODE),
        StaticRewriter([]),
        FakeAnswerClient("伪造引用 [C9]。"),
    )

    with pytest.raises(InvalidCitationError):
        runner.run("代码在哪里？", 5)


def test_new_results_are_first_and_ids_are_stably_deduplicated() -> None:
    old = [result(1, "CODE"), result(2, "CODE")]
    new = [result(2, "CODE"), result(3, "CODE")]

    assert [item.id for item in _merge_results(new, old)] == [2, 3, 1]


def test_retry_limit_cannot_exceed_v1_cap() -> None:
    with pytest.raises(ValueError, match="between 0 and 2"):
        AgenticRetrievalWorkflow(
            router=FakeRouter(decision(QueryType.CODE)),  # type: ignore[arg-type]
            retrieval_policy=FakePolicy([]),  # type: ignore[arg-type]
            context_builder=ContextBuilder(),
            sufficiency_checker=SourceAwareChecker(QueryType.CODE),  # type: ignore[arg-type]
            query_rewriter=StaticRewriter([]),  # type: ignore[arg-type]
            answer_generator_factory=lambda: AnswerGenerator(
                FakeAnswerClient("unused")
            ),
            max_retries=3,
        )
