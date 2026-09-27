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
from devcontext.answer import SECTION_MAX_CHARS
from devcontext.context import ContextBuilder
from devcontext.models import AnswerResult, SearchExecution, SearchResult, SearchTimings
from devcontext.planning import (
    EvidencePlan,
    EvidenceRequirement,
    QuestionPlan,
    SubQuestion,
)
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


def plan(*questions: str, decision_source: str = "llm") -> QuestionPlan:
    return QuestionPlan(
        original_query=QUERY,
        intent_summary="了解订单关闭",
        sub_questions=tuple(
            SubQuestion(f"SQ{index}", question, "purpose")
            for index, question in enumerate(questions, start=1)
        ),
        answer_depth="detailed",
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


class FakeRouter:
    def __init__(self, routes: dict[str, QueryType]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def route(self, query: str) -> RouteDecision:
        self.calls.append(query)
        return decision(self.routes[query])


class FakeEvidencePlanner:
    """Defaults to a fallback plan so the pre-Evidence-Planner path stays the baseline."""

    def __init__(self, plan: EvidencePlan | None = None) -> None:
        self.plan_result = plan or EvidencePlan(
            requirements=(), decision_source="fallback"
        )
        self.calls: list[QuestionPlan] = []

    def plan(self, question_plan: QuestionPlan) -> EvidencePlan:
        self.calls.append(question_plan)
        return self.plan_result


def evidence_plan(*source_lists: list[str]) -> EvidencePlan:
    return EvidencePlan(
        requirements=tuple(
            EvidenceRequirement(
                id=f"ER{index}",
                sub_question_id=f"SQ{index}",
                description=f"requirement {index}",
                preferred_sources=tuple(sources),
            )
            for index, sources in enumerate(source_lists, start=1)
        ),
        decision_source="llm",
    )


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
    """Records which entry point ran, so tests can assert the branch taken.

    Both entry points share one result queue unless `requirement_results` is given.
    """

    def __init__(
        self,
        results: list[SufficiencyResult],
        requirement_results: list[SufficiencyResult] | None = None,
    ) -> None:
        self.results = list(results)
        self.requirement_results = (
            None if requirement_results is None else list(requirement_results)
        )
        self.legacy_calls = 0
        self.requirement_calls: list[tuple[list[object], dict[str, list[int]]]] = []

    @staticmethod
    def _next(queue: list[SufficiencyResult]) -> SufficiencyResult:
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def check(
        self, query: str, route: RouteDecision, bundle: object
    ) -> SufficiencyResult:
        self.legacy_calls += 1
        return self._next(self.results)

    def check_requirements(
        self,
        query: str,
        requirements: object,
        evidence_index: object,
        bundle: object,
    ) -> SufficiencyResult:
        self.requirement_calls.append(
            (
                list(requirements),  # type: ignore[arg-type]
                # Snapshot the lists too: retry attribution mutates them in place.
                {
                    key: list(value)
                    for key, value in evidence_index.items()  # type: ignore[union-attr]
                },
            )
        )
        queue = (
            self.results
            if self.requirement_results is None
            else self.requirement_results
        )
        return self._next(queue)


class FakeRewriter:
    def __init__(self, rewrite: RewriteResult | Exception) -> None:
        self.rewrite_result = rewrite
        self.calls = 0

    def rewrite(self, query, route, sufficiency, bundle):
        self.calls += 1
        if isinstance(self.rewrite_result, Exception):
            raise self.rewrite_result
        return self.rewrite_result


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


def insufficient() -> SufficiencyResult:
    return SufficiencyResult(
        False, (MissingAspect("CODE", "缺少代码"),), "missing code", "llm"
    )


def workflow(
    planned: QuestionPlan,
    routes: dict[str, QueryType],
    responses: dict[str, list[SearchResult]],
    sufficiency: list[SufficiencyResult],
    rewriter: FakeRewriter | None = None,
    generator: FakeGenerator | None = None,
    evidence: FakeEvidencePlanner | None = None,
    max_sub_questions: int = 6,
    sub_question_top_k: int = 3,
    max_rewrites: int = 1,
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
        evidence_planner=evidence or FakeEvidencePlanner(),  # type: ignore[arg-type]
        router=FakeRouter(routes),  # type: ignore[arg-type]
        retrieval_policy=policy,  # type: ignore[arg-type]
        context_builder=ContextBuilder(),
        sufficiency_checker=checker,  # type: ignore[arg-type]
        query_rewriter=query_rewriter,  # type: ignore[arg-type]
        legacy_workflow=legacy,  # type: ignore[arg-type]
        answer_generator_factory=lambda: generator,  # type: ignore[arg-type]
        max_sub_questions=max_sub_questions,
        sub_question_top_k=sub_question_top_k,
        max_rewrites=max_rewrites,
    )
    return subject, policy, legacy, generator, query_rewriter, checker


def test_each_sub_question_is_searched_independently() -> None:
    subject, policy, _, _, _, _ = workflow(
        plan("入口在哪里", "设计依据是什么"),
        {"入口在哪里": QueryType.CODE, "设计依据是什么": QueryType.DOC},
        {
            "入口在哪里": [result(1, "CODE")],
            "设计依据是什么": [result(2, "DOCUMENT")],
        },
        [enough()],
    )

    output = subject.run(QUERY, 5)

    assert policy.calls == [
        ("入口在哪里", QueryType.CODE, 3),
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


def test_results_are_merged_with_earlier_sub_questions_first() -> None:
    subject, _, _, _, _, _ = workflow(
        plan("入口在哪里", "设计依据是什么"),
        {"入口在哪里": QueryType.CODE, "设计依据是什么": QueryType.CODE},
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


def test_sub_questions_are_capped() -> None:
    subject, policy, _, _, _, _ = workflow(
        plan(*[f"问题{index}" for index in range(8)]),
        {f"问题{index}": QueryType.CODE for index in range(8)},
        {f"问题{index}": [result(index + 1, "CODE")] for index in range(8)},
        [enough()],
        max_sub_questions=3,
    )

    subject.run(QUERY, 5)

    assert len(policy.calls) == 3


def test_fallback_plan_delegates_to_the_legacy_workflow() -> None:
    subject, policy, legacy, generator, _, _ = workflow(
        plan(QUERY, decision_source="fallback"),
        {QUERY: QueryType.CODE},
        {QUERY: [result(1, "CODE")]},
        [enough()],
    )

    output = subject.run(QUERY, 7)

    assert legacy.calls == [(QUERY, 7)]
    assert policy.calls == []
    assert generator.generate_calls == []
    assert output.answer_result.answer == "legacy"
    assert output.trace.plan["decision_source"] == "fallback"


def test_insufficient_evidence_triggers_one_targeted_rewrite() -> None:
    subject, policy, _, generator, rewriter, _ = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
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
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
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
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
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
        plan("入口在哪里", "设计依据是什么"),
        {"入口在哪里": QueryType.CODE, "设计依据是什么": QueryType.DOC},
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
    assert "Depth: detailed" in outline
    assert "1. 入口在哪里" in outline
    assert "2. 设计依据是什么" in outline


def test_evidence_requirements_choose_the_retrieval_route() -> None:
    evidence = FakeEvidencePlanner(evidence_plan(["CODE"], ["DOCUMENT"]))
    subject, policy, _, _, _, _ = workflow(
        plan("入口在哪里", "设计依据是什么"),
        {"入口在哪里": QueryType.CODE, "设计依据是什么": QueryType.DOC},
        {
            "入口在哪里": [result(1, "CODE")],
            "设计依据是什么": [result(2, "DOCUMENT")],
        },
        [enough()],
        evidence=evidence,
    )

    output = subject.run(QUERY, 5)

    assert [call[1] for call in policy.calls] == [QueryType.CODE, QueryType.DOC]
    assert output.trace.route.query_type is QueryType.MIXED
    assert output.trace.evidence_plan is not None
    assert output.trace.evidence_plan["decision_source"] == "llm"
    assert len(output.trace.evidence_plan["requirements"]) == 2
    assert len(evidence.calls) == 1


def test_two_sources_in_one_requirement_route_as_mixed() -> None:
    subject, policy, _, _, _, _ = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
        evidence=FakeEvidencePlanner(evidence_plan(["CODE", "DOCUMENT"])),
    )

    subject.run(QUERY, 5)

    assert [call[1] for call in policy.calls] == [QueryType.MIXED]


def test_evidence_plan_failure_falls_back_to_the_router() -> None:
    subject, policy, _, _, _, _ = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.DOC},
        {"入口在哪里": [result(1, "DOCUMENT")]},
        [enough()],
        evidence=FakeEvidencePlanner(),
    )

    output = subject.run(QUERY, 5)

    assert [call[1] for call in policy.calls] == [QueryType.DOC]
    assert output.trace.evidence_plan == {
        "decision_source": "fallback",
        "requirements": [],
    }


def test_evidence_planner_is_not_called_when_the_question_plan_falls_back() -> None:
    evidence = FakeEvidencePlanner(evidence_plan(["CODE"]))
    subject, policy, legacy, _, _, _ = workflow(
        plan(QUERY, decision_source="fallback"),
        {QUERY: QueryType.CODE},
        {QUERY: [result(1, "CODE")]},
        [enough()],
        evidence=evidence,
    )

    subject.run(QUERY, 5)

    assert evidence.calls == []
    assert policy.calls == []
    assert legacy.calls == [(QUERY, 5)]


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
    requirement = EvidenceRequirement(
        id="ER1",
        sub_question_id="SQ1",
        description="d",
        preferred_sources=tuple(sources),
    )

    assert to_route_decision(requirement).query_type is expected


def test_evidence_plan_uses_the_per_requirement_checker() -> None:
    subject, _, _, _, _, checker = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
        evidence=FakeEvidencePlanner(evidence_plan(["CODE"])),
    )

    subject.run(QUERY, 5)

    assert checker.legacy_calls == 0
    assert len(checker.requirement_calls) == 1
    requirements, evidence_index = checker.requirement_calls[0]
    assert [item.id for item in requirements] == ["ER1"]
    assert evidence_index == {"ER1": [1]}


def test_fallback_evidence_plan_uses_the_legacy_checker() -> None:
    subject, _, _, _, _, checker = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
        evidence=FakeEvidencePlanner(),  # fallback: no requirements
    )

    subject.run(QUERY, 5)

    assert checker.requirement_calls == []
    assert checker.legacy_calls == 1


def test_requirements_follow_the_sub_question_slice() -> None:
    """A truncated sub-question must not leave its requirement behind."""
    subject, policy, _, _, _, checker = workflow(
        plan("入口在哪里", "设计依据是什么", "还有别的吗"),
        {
            "入口在哪里": QueryType.CODE,
            "设计依据是什么": QueryType.CODE,
            "还有别的吗": QueryType.CODE,
        },
        {
            "入口在哪里": [result(1, "CODE")],
            "设计依据是什么": [result(2, "CODE")],
            "还有别的吗": [result(3, "CODE")],
        },
        [enough()],
        evidence=FakeEvidencePlanner(
            evidence_plan(["CODE"], ["CODE"], ["CODE"])
        ),
        max_sub_questions=2,
    )

    subject.run(QUERY, 5)

    assert len(policy.calls) == 2
    requirements, evidence_index = checker.requirement_calls[0]
    assert [item.id for item in requirements] == ["ER1", "ER2"]
    assert "ER3" not in evidence_index


def test_retry_results_are_attributed_to_the_targeted_requirements() -> None:
    missing = MissingAspect("CODE", "缺少入口证据", "ER1")
    subject, _, _, _, _, checker = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
        {"入口在哪里": [result(1, "CODE")], "精确检索": [result(2, "CODE")]},
        [SufficiencyResult(False, (missing,), "不足", "llm"), enough()],
        rewriter=FakeRewriter(
            RewriteResult(QUERY, "精确检索", QueryType.CODE, (missing,))
        ),
        evidence=FakeEvidencePlanner(evidence_plan(["CODE"])),
    )

    subject.run(QUERY, 5)

    assert len(checker.requirement_calls) == 2
    _, first_index = checker.requirement_calls[0]
    _, second_index = checker.requirement_calls[1]
    assert first_index == {"ER1": [1]}
    assert second_index == {"ER1": [1, 2]}


def test_unattributed_aspects_do_not_add_evidence() -> None:
    """An aspect without a requirement id (legacy shape) must not invent an entry."""
    missing = MissingAspect("CODE", "缺少证据")
    subject, _, _, _, _, checker = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
        {"入口在哪里": [result(1, "CODE")], "精确检索": [result(2, "CODE")]},
        [SufficiencyResult(False, (missing,), "不足", "llm"), enough()],
        rewriter=FakeRewriter(
            RewriteResult(QUERY, "精确检索", QueryType.CODE, (missing,))
        ),
        evidence=FakeEvidencePlanner(evidence_plan(["CODE"])),
    )

    subject.run(QUERY, 5)

    _, first_index = checker.requirement_calls[0]
    _, second_index = checker.requirement_calls[1]
    assert second_index == first_index == {"ER1": [1]}


def test_section_lengths_are_measured_into_the_trace() -> None:
    answer = "## 第一节\n\n" + "字" * 200 + "\n\n## 第二节\n\n" + "字" * 400
    subject, _, _, _, _, _ = workflow(
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
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
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
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
        plan("入口在哪里"),
        {"入口在哪里": QueryType.CODE},
        {"入口在哪里": [result(1, "CODE")]},
        [enough()],
    )

    with pytest.raises(ValueError):
        subject.run("   ", 5)
