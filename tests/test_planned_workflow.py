from __future__ import annotations

import pytest

from devcontext.agentic import (
    AgenticAnswerResult,
    AgenticTrace,
    PlannedRetrievalWorkflow,
    QueryRewriteError,
    RewriteResult,
    SufficiencyResult,
    to_route_decision,
    union_route,
)
from devcontext.agentic.models import MissingAspect
from devcontext.answer import (
    SECTION_MAX_CHARS,
    AnswerPlan,
    AnswerSection,
    GroundedDraft,
    ReviewResult,
)
from devcontext.context import ContextBuilder
from devcontext.models import AnswerResult, SearchExecution, SearchResult, SearchTimings
from devcontext.planning import QuestionPlan, SubQuestion
from devcontext.routing import DecisionSource, QueryType, RouteDecision

QUERY = "订单关闭是怎么实现的，为什么这样设计？"


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


def sq(question: str, *sources: str) -> tuple[str, tuple[str, ...]]:
    return (question, tuple(sources) or ("CODE",))


def plan(*entries: tuple[str, tuple[str, ...]], decision_source: str = "llm") -> QuestionPlan:
    """Sub-questions carry their own evidence sources, so they also drive routing."""
    return QuestionPlan(
        original_query=QUERY,
        intent_summary="了解订单关闭",
        sub_questions=tuple(
            SubQuestion(
                f"SQ{index}",
                question,
                "purpose",
                f"{question} 要找的证据",
                sources,
                question,
                "CORE" if index == 1 else "SUPPORTING",
                "CURRENT",
            )
            for index, (question, sources) in enumerate(entries, start=1)
        ),

        decision_source=decision_source,
    )


def decision(query_type: QueryType) -> RouteDecision:
    return RouteDecision(query_type, DecisionSource.RULES, "test")


class FakePlanner:
    def __init__(self, planned: QuestionPlan) -> None:
        self.planned = planned
        self.calls: list[str] = []

    def plan(self, query: str) -> QuestionPlan:
        self.calls.append(query)
        return self.planned


class FakePolicy:
    def __init__(self, responses: dict[str, list[SearchResult]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, QueryType, int]] = []

    def search_with_trace(
        self, query: str, route: RouteDecision, top_k: int
    ) -> SearchExecution:
        self.calls.append((query, route.query_type, top_k))
        return SearchExecution(list(self.responses.get(query, [])), SearchTimings())


class FakeSufficiency:
    """Records the sub-questions and the evidence index it was handed."""

    def __init__(self, results: list[SufficiencyResult]) -> None:
        self.results = list(results)
        self.legacy_calls = 0
        self.sub_question_calls: list[tuple[list[object], dict[str, list[int]]]] = []

    def _next(self) -> SufficiencyResult:
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

    def check(
        self, query: str, route: RouteDecision, bundle: object
    ) -> SufficiencyResult:
        self.legacy_calls += 1
        return self._next()

    def check_sub_questions(
        self,
        query: str,
        sub_questions: object,
        evidence_index: object,
        bundle: object,
    ) -> SufficiencyResult:
        self.sub_question_calls.append(
            (
                list(sub_questions),  # type: ignore[arg-type]
                # Snapshot the lists too: retry attribution mutates them in place.
                {
                    key: list(value)
                    for key, value in evidence_index.items()  # type: ignore[union-attr]
                },
            )
        )
        return self._next()


class FakeRewriter:
    def __init__(self, rewrite: RewriteResult | Exception) -> None:
        self.rewrite_result = rewrite
        self.calls = 0

    def rewrite(self, query, route, sufficiency, bundle):
        self.calls += 1
        if isinstance(self.rewrite_result, Exception):
            raise self.rewrite_result
        return self.rewrite_result


class PerAspectRewriter:
    def __init__(self) -> None:
        self.target_ids: list[str] = []

    def rewrite(self, query, route, sufficiency, bundle):
        target = sufficiency.missing_aspects[0].sub_question_id
        self.target_ids.append(target)
        return RewriteResult(
            query,
            f"retry-{target}",
            QueryType.CODE,
            sufficiency.missing_aspects,
        )


class FakeGenerator:
    def __init__(self, answer: str = "完整回答") -> None:
        self.answer = answer
        self.generate_calls: list[str | None] = []
        self.partial_calls: list[tuple[list[str], str | None]] = []

    def generate(self, query, bundle, outline=None) -> AnswerResult:
        self.generate_calls.append(outline)
        return AnswerResult(
            answer=self.answer, used_citations=[], zero_valid_citation=True
        )

    def generate_partial(self, query, bundle, missing, outline=None) -> AnswerResult:
        self.partial_calls.append((list(missing), outline))
        return AnswerResult(
            answer=self.answer, used_citations=[], zero_valid_citation=True
        )


class FakeLegacy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def run(self, query: str, top_k: int):
        self.calls.append((query, top_k))
        trace = AgenticTrace(
            route=decision(QueryType.CODE),
            rounds=[],
            retry_count=0,
            final_sufficiency=SufficiencyResult(True, (), "ok", "rules"),
            stop_reason="sufficient",
        )
        return AgenticAnswerResult(
            AnswerResult(answer="legacy", used_citations=[]),
            ContextBuilder().build(QUERY, []),
            trace,
        )


def enough() -> SufficiencyResult:
    return SufficiencyResult(True, (), "sufficient", "llm")


def insufficient(sub_question_id: str = "") -> SufficiencyResult:
    return SufficiencyResult(
        False,
        (MissingAspect("CODE", "缺少代码", sub_question_id),),
        "missing code",
        "llm",
    )


def workflow(
    planned: QuestionPlan,
    responses: dict[str, list[SearchResult]],
    sufficiency: list[SufficiencyResult],
    rewriter: FakeRewriter | None = None,
    generator: FakeGenerator | None = None,
    max_sub_questions: int = 6,
    sub_question_top_k: int = 3,
    max_rewrites: int = 1,
    depth_override: str | None = None,
    answer_mode: str = "legacy",
) -> tuple[
    PlannedRetrievalWorkflow,
    FakePolicy,
    FakeLegacy,
    FakeGenerator,
    FakeRewriter,
    FakeSufficiency,
]:
    policy = FakePolicy(responses)
    legacy = FakeLegacy()
    generator = generator or FakeGenerator()
    checker = FakeSufficiency(sufficiency)
    query_rewriter = rewriter or FakeRewriter(
        QueryRewriteError("must not be called")
    )
    subject = PlannedRetrievalWorkflow(
        planner=FakePlanner(planned),  # type: ignore[arg-type]
        retrieval_policy=policy,  # type: ignore[arg-type]
        context_builder=ContextBuilder(),
        sufficiency_checker=checker,  # type: ignore[arg-type]
        query_rewriter=query_rewriter,  # type: ignore[arg-type]
        legacy_workflow=legacy,  # type: ignore[arg-type]
        answer_generator_factory=lambda: generator,  # type: ignore[arg-type]
        max_sub_questions=max_sub_questions,
        sub_question_top_k=sub_question_top_k,
        max_rewrites=max_rewrites,
        answer_mode=answer_mode,
    )
    return subject, policy, legacy, generator, query_rewriter, checker


def test_each_sub_question_is_searched_independently() -> None:
    subject, policy, _, _, _, _ = workflow(
        plan(sq("入口在哪里", "CODE"), sq("设计依据是什么", "DOCUMENT")),
        {
            "入口在哪里": [result(1, "CODE")],
            "设计依据是什么": [result(2, "DOCUMENT")],
        },
        [enough()],
    )

    output = subject.run(QUERY, 5)

    assert policy.calls == [
        ("入口在哪里", QueryType.CODE, 5),
        ("设计依据是什么", QueryType.DOC, 3),
    ]
    assert output.trace.route.query_type is QueryType.MIXED
    assert [trace.sub_question_id for trace in output.trace.sub_question_traces] == [
        "SQ1",
        "SQ2",
    ]
    assert output.trace.sub_question_traces[0].query_type == "CODE"
    assert output.trace.sub_question_traces[1].query_type == "DOC"
    assert len(output.context_bundle.items) == 2
    # The plan now carries the evidence needs, so there is no separate plan block.
    assert output.trace.plan is not None
    assert (
        output.trace.plan["sub_questions"][0]["preferred_sources"] == ["CODE"]
    )
    assert "evidence_plan" not in output.trace.to_dict()


def test_two_sources_in_one_sub_question_route_as_mixed() -> None:
    subject, policy, _, _, _, _ = workflow(
        plan(sq("入口在哪里", "CODE", "DOCUMENT")),
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
    )

    subject.run(QUERY, 5)

    assert [call[1] for call in policy.calls] == [QueryType.MIXED]


def test_results_are_merged_with_earlier_sub_questions_first() -> None:
    subject, _, _, _, _, _ = workflow(
        plan(sq("入口在哪里"), sq("设计依据是什么")),
        {
            "入口在哪里": [result(3, "CODE"), result(1, "CODE")],
            "设计依据是什么": [result(1, "CODE"), result(2, "CODE")],
        },
        [enough()],
    )

    output = subject.run(QUERY, 5)

    # Sub-questions keep their planned order; the second one only adds what the first
    # did not already cover.
    assert [item.chunk_id for item in output.context_bundle.items] == [3, 1, 2]


def test_sub_questions_are_capped_before_retrieval() -> None:
    subject, policy, _, _, _, checker = workflow(
        plan(*[sq(f"问题{index}") for index in range(8)]),
        {f"问题{index}": [result(index + 1, "CODE")] for index in range(8)},
        [enough()],
        max_sub_questions=2,
    )

    subject.run(QUERY, 5)

    assert len(policy.calls) == 2
    sub_questions, evidence_index = checker.sub_question_calls[0]
    assert [item.id for item in sub_questions] == ["SQ1", "SQ2"]
    assert sorted(evidence_index) == ["SQ1", "SQ2"]


def test_fallback_plan_delegates_to_the_legacy_workflow() -> None:
    subject, policy, legacy, generator, _, checker = workflow(
        plan(sq(QUERY), decision_source="fallback"),
        {QUERY: [result(1, "CODE")]},
        [enough()],
    )

    output = subject.run(QUERY, 7)

    assert legacy.calls == [(QUERY, 7)]
    assert policy.calls == []
    assert checker.sub_question_calls == []
    assert generator.generate_calls == []
    assert output.answer_result.answer == "legacy"
    assert output.trace.plan["decision_source"] == "fallback"


def test_insufficient_evidence_triggers_one_targeted_rewrite() -> None:
    subject, policy, _, generator, rewriter, _ = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")], "Service run 精确代码": [result(2, "CODE")]},
        [insufficient(), enough()],
        rewriter=FakeRewriter(
            RewriteResult(QUERY, "Service run 精确代码", QueryType.CODE, ())
        ),
    )

    output = subject.run(QUERY, 5)

    assert rewriter.calls == 1
    assert policy.calls[-1] == ("Service run 精确代码", QueryType.CODE, 5)
    assert output.trace.retry_count == 1
    assert output.trace.stop_reason == "sufficient"
    assert len(generator.generate_calls) == 1


def test_rewrite_failure_stops_with_a_partial_answer() -> None:
    subject, _, _, generator, _, _ = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")]},
        [insufficient()],
        rewriter=FakeRewriter(QueryRewriteError("rewrite exploded")),
    )

    output = subject.run(QUERY, 5)

    assert output.trace.stop_reason == "rewrite_failed"
    assert output.trace.retry_count == 0
    assert len(generator.partial_calls) == 1


def test_empty_context_skips_the_answer_model() -> None:
    subject, _, _, generator, _, _ = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": []},
        [insufficient()],
    )

    output = subject.run(QUERY, 5)

    assert output.trace.stop_reason == "empty_context"
    assert generator.generate_calls == []
    assert generator.partial_calls == []
    assert "当前没有检索到足够的项目上下文" in output.answer_result.answer


def test_answer_receives_the_plan_outline() -> None:
    subject, _, _, generator, _, _ = workflow(
        plan(sq("入口在哪里", "CODE"), sq("设计依据是什么", "DOCUMENT")),
        {
            "入口在哪里": [result(1, "CODE")],
            "设计依据是什么": [result(2, "DOCUMENT")],
        },
        [enough()],
    )

    subject.run(QUERY, 5)

    outline = generator.generate_calls[0]
    assert outline is not None
    assert "Answer Outline:" in outline
    assert "Depth:" not in outline
    assert "1. 入口在哪里" in outline
    assert "2. 设计依据是什么" in outline


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (["CODE"], QueryType.CODE),
        (["DOCUMENT"], QueryType.DOC),
        (["CODE", "DOCUMENT"], QueryType.MIXED),
        (["DOCUMENT", "CODE"], QueryType.MIXED),
    ],
)
def test_to_route_decision_maps_preferred_sources(
    sources: list[str], expected: QueryType
) -> None:
    sub_question = SubQuestion(
        "SQ1", "q", "p", "evidence", tuple(sources)
    )

    assert to_route_decision(sub_question).query_type is expected


def test_sub_questions_go_to_the_per_sub_question_checker() -> None:
    subject, _, _, _, _, checker = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
    )

    subject.run(QUERY, 5)

    assert checker.legacy_calls == 0
    assert len(checker.sub_question_calls) == 1
    sub_questions, evidence_index = checker.sub_question_calls[0]
    assert [item.id for item in sub_questions] == ["SQ1"]
    assert evidence_index == {"SQ1": [1]}


def test_retry_results_are_attributed_to_the_targeted_sub_questions() -> None:
    missing = MissingAspect("CODE", "缺少入口证据", "SQ1")
    subject, _, _, _, _, checker = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")], "精确检索": [result(2, "CODE")]},
        [SufficiencyResult(False, (missing,), "不足", "llm"), enough()],
        rewriter=FakeRewriter(
            RewriteResult(QUERY, "精确检索", QueryType.CODE, (missing,))
        ),
    )

    subject.run(QUERY, 5)

    assert len(checker.sub_question_calls) == 2
    _, first_index = checker.sub_question_calls[0]
    _, second_index = checker.sub_question_calls[1]
    assert first_index == {"SQ1": [1]}
    assert second_index == {"SQ1": [1, 2]}


def test_one_retry_round_uses_at_most_four_independent_search_tasks() -> None:
    entries = tuple(sq(f"问题{index}") for index in range(1, 6))
    missing = tuple(
        MissingAspect("CODE", f"缺口 {index}", f"SQ{index}")
        for index in range(1, 6)
    )
    responses = {
        **{f"问题{index}": [result(index, "CODE")] for index in range(1, 6)},
        "retry-SQ1": [result(11, "CODE")],
        **{
            f"问题{index} 缺口 {index} void run()": [result(index + 10, "CODE")]
            for index in range(2, 5)
        },
    }
    rewriter = PerAspectRewriter()
    subject, policy, _, _, _, _ = workflow(
        plan(*entries),
        responses,
        [SufficiencyResult(False, missing, "缺证据", "llm"), enough()],
        rewriter=rewriter,  # type: ignore[arg-type]
    )

    output = subject.run(QUERY, 12)

    assert rewriter.target_ids == ["SQ1"]
    assert len(policy.calls) == 9  # five initial tasks plus four targeted tasks
    assert len(output.trace.rounds[0].rewrites) == 4
    assert output.trace.retry_count == 1


def test_unattributed_aspects_do_not_add_evidence() -> None:
    """An aspect without a sub-question id (legacy shape) must not invent an entry."""
    missing = MissingAspect("CODE", "缺少证据")
    subject, _, _, _, _, checker = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")], "精确检索": [result(2, "CODE")]},
        [SufficiencyResult(False, (missing,), "不足", "llm"), enough()],
        rewriter=FakeRewriter(
            RewriteResult(QUERY, "精确检索", QueryType.CODE, (missing,))
        ),
    )

    subject.run(QUERY, 5)

    _, first_index = checker.sub_question_calls[0]
    _, second_index = checker.sub_question_calls[1]
    assert second_index == first_index == {"SQ1": [1]}


def test_section_lengths_are_measured_into_the_trace() -> None:
    answer = "## 第一节\n\n" + "字" * 200 + "\n\n## 第二节\n\n" + "字" * 400
    subject, _, _, _, _, _ = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
        generator=FakeGenerator(answer),
    )

    output = subject.run(QUERY, 5)

    assert output.trace.sections is not None
    assert output.trace.sections["count"] == 2
    assert output.trace.sections["max_chars"] > SECTION_MAX_CHARS
    assert output.trace.sections["over_budget"] == 1


def test_citations_are_recorded_in_the_trace() -> None:
    subject, _, _, _, _, _ = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
    )

    output = subject.run(QUERY, 5)

    assert output.trace.citations == {
        "used_labels": [],
        "invalid_labels": [],
        "zero_valid": True,
        "sources": [],
    }


@pytest.mark.parametrize(
    ("routes", "expected"),
    [
        ([QueryType.CODE, QueryType.DOC], QueryType.MIXED),
        ([QueryType.CODE, QueryType.CODE], QueryType.CODE),
        ([QueryType.DOC, QueryType.DOC], QueryType.DOC),
        ([QueryType.MIXED], QueryType.MIXED),
        ([QueryType.CODE, QueryType.MIXED], QueryType.MIXED),
        ([], QueryType.MIXED),
    ],
)
def test_union_route_collapses_sub_question_evidence_needs(
    routes: list[QueryType], expected: QueryType
) -> None:
    assert union_route([decision(route) for route in routes]).query_type is expected


def test_empty_query_is_rejected() -> None:
    subject, _, _, _, _, _ = workflow(
        plan(sq("入口在哪里")),
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
    )

    with pytest.raises(ValueError):
        subject.run("   ", 5)


class FakeExplainGenerator:
    def __init__(self) -> None:
        self.calls = 0
        self.client = type("ClientStats", (), {
            "model": "answer-model",
            "reasoning_effort": "high",
            "last_usage": {"prompt_tokens": 11, "completion_tokens": 7},
        })()

    def generate_explained_draft(self, query, bundle, answer_plan):
        self.calls += 1
        return GroundedDraft("先直接回答。 [C1]", ("C1",), ())

    @staticmethod
    def finalize_draft(draft: GroundedDraft) -> AnswerResult:
        return AnswerResult(
            draft.text_with_citations.replace(" [C1]", ""),
            list(draft.used_citations),
        )


class FakeAnswerPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, query, question_plan, bundle, gaps):
        self.calls += 1
        return AnswerPlan(
            "当前实现有明确主线。",
            ("C1",),
            "flow",
            (
                AnswerSection("主流程", "解释顺序", ("当前实现",), ("C1",), 800),
                AnswerSection("边界", "解释边界", ("保证范围",), ("C1",), 700),
                AnswerSection("失败场景", "解释失败", ("失败行为",), ("C1",), 700),
            ),
            (),
            (),
        )


class FakeReviewer:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, query, question_plan, answer_plan, draft, bundle):
        self.calls += 1
        return ReviewResult(True, (), "审稿后的回答。 [C1]")


def test_explain_mode_plans_drafts_and_reviews_detailed_answers() -> None:
    planned = plan(sq("入口在哪里"))
    policy = FakePolicy({"入口在哪里": [result(1, "CODE")]})
    generator = FakeExplainGenerator()
    answer_planner = FakeAnswerPlanner()
    reviewer = FakeReviewer()
    subject = PlannedRetrievalWorkflow(
        planner=FakePlanner(planned),
        retrieval_policy=policy,
        context_builder=ContextBuilder(),
        sufficiency_checker=FakeSufficiency([enough()]),
        query_rewriter=FakeRewriter(QueryRewriteError("unused")),
        legacy_workflow=FakeLegacy(),
        answer_generator_factory=lambda: generator,  # type: ignore[arg-type]
        answer_planner=answer_planner,  # type: ignore[arg-type]
        answer_reviewer=reviewer,  # type: ignore[arg-type]
        answer_mode="explain",
    )

    output = subject.run(QUERY, 5)

    assert generator.calls == 1
    assert answer_planner.calls == 1
    assert reviewer.calls == 1
    assert output.answer_result.answer == "审稿后的回答。"
    assert output.trace.answer_plan is not None
    assert output.trace.review is not None
    assert output.trace.rounds[0].selected_chunks[0].source_role == "IMPLEMENTATION"
    assert output.trace.rounds[0].selected_chunks[0].temporal_status == "CURRENT"
    assert {item.stage for item in output.trace.stage_usage} >= {
        "investigation_planning", "evidence_retrieval", "sufficiency",
        "answer_planning", "grounded_draft", "answer_review",
    }
    draft_usage = next(
        item for item in output.trace.stage_usage if item.stage == "grounded_draft"
    )
    assert draft_usage.model == "answer-model"
    assert draft_usage.input_tokens == 11
    assert draft_usage.output_tokens == 7


def test_historical_brief_does_not_skip_explanation_planning() -> None:
    planned = plan(sq("入口在哪里"))
    planned = QuestionPlan(
        planned.original_query,
        planned.intent_summary,
        planned.sub_questions,
        "brief",
        answer_goal="定位入口",
    )
    policy = FakePolicy({"入口在哪里": [result(1, "CODE")]})
    generator = FakeExplainGenerator()
    answer_planner = FakeAnswerPlanner()
    reviewer = FakeReviewer()
    subject = PlannedRetrievalWorkflow(
        planner=FakePlanner(planned), retrieval_policy=policy,
        context_builder=ContextBuilder(),
        sufficiency_checker=FakeSufficiency([enough()]),
        query_rewriter=FakeRewriter(QueryRewriteError("unused")),
        legacy_workflow=FakeLegacy(),
        answer_generator_factory=lambda: generator,  # type: ignore[arg-type]
        answer_planner=answer_planner,  # type: ignore[arg-type]
        answer_reviewer=reviewer,  # type: ignore[arg-type]
        answer_mode="explain",
    )

    subject.run(QUERY, 5)

    assert answer_planner.calls == 1
    assert reviewer.calls == 1


def test_runtime_plan_has_no_depth_field() -> None:
    subject, _, _, _, _, _ = workflow(
        plan(sq("入口在哪里", "CODE"), sq("设计依据是什么", "DOCUMENT")),
        {
            "入口在哪里": [result(1, "CODE")],
            "设计依据是什么": [result(2, "DOCUMENT")],
        },
        [enough()],
        )

    output = subject.run(QUERY, 5)

    # The plan was authored as detailed; the override wins and drives the budget.
    assert output.trace.plan is not None
    assert "answer_depth" not in output.trace.plan
    assert output.context_bundle.max_chars == 28000


def test_context_budget_is_independent_of_historical_depth() -> None:
    subject, _, _, _, _, _ = workflow(
        plan(sq("入口在哪里", "CODE")),
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
    )

    output = subject.run(QUERY, 5)

    assert output.trace.plan is not None
    assert "answer_depth" not in output.trace.plan
    assert output.context_bundle.max_chars == 28000


def test_removed_programmatic_depth_override_is_rejected() -> None:
    with pytest.raises(TypeError, match="depth_override"):
        PlannedRetrievalWorkflow(depth_override="verbose", planner=None, retrieval_policy=None, context_builder=None, sufficiency_checker=None, query_rewriter=None, legacy_workflow=None, answer_generator_factory=None)
