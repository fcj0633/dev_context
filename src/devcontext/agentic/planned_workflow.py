from __future__ import annotations

from collections.abc import Callable, Sequence

from devcontext.agentic.models import (
    AgenticAnswerResult,
    AgenticRoundTrace,
    AgenticTrace,
    MissingAspect,
    SelectedChunkTrace,
    SubQuestionTrace,
    build_citation_trace,
)
from devcontext.agentic.rewrite import QueryRewriteError, TargetedQueryRewriter
from devcontext.agentic.sufficiency import ContextSufficiencyChecker
from devcontext.agentic.workflow import (
    AgenticRetrievalWorkflow,
    _format_missing_aspects,
    _merge_results,
)
from devcontext.answer import (
    EMPTY_CONTEXT_ANSWER,
    AnswerGenerator,
    describe_sections,
)
from devcontext.context import ContextBuilder
from devcontext.models import AnswerResult, ContextBundle, SearchResult
from devcontext.planning import (
    MAX_SUB_QUESTIONS,
    QuestionPlan,
    QuestionPlanner,
    SubQuestion,
)
from devcontext.retrieval import RetrievalPolicy
from devcontext.routing import DecisionSource, QueryType, RouteDecision


SUB_QUESTION_TOP_K = 3
MAX_REWRITES = 1


class PlannedRetrievalWorkflow:
    """Question-plan driven retrieval, falling back to the legacy workflow.

    Fan-out happens per sub-question; the union of sub-question routes becomes the
    evidence requirement handed to the existing sufficiency checker.
    """

    def __init__(
        self,
        planner: QuestionPlanner,
        retrieval_policy: RetrievalPolicy,
        context_builder: ContextBuilder,
        sufficiency_checker: ContextSufficiencyChecker,
        query_rewriter: TargetedQueryRewriter,
        legacy_workflow: AgenticRetrievalWorkflow,
        answer_generator_factory: Callable[[], AnswerGenerator],
        max_sub_questions: int = MAX_SUB_QUESTIONS,
        sub_question_top_k: int = SUB_QUESTION_TOP_K,
        max_rewrites: int = MAX_REWRITES,
    ) -> None:
        if max_sub_questions < 1:
            raise ValueError("max_sub_questions must be positive")
        if sub_question_top_k < 1:
            raise ValueError("sub_question_top_k must be positive")
        if max_rewrites < 0:
            raise ValueError("max_rewrites must not be negative")
        self.planner = planner
        self.retrieval_policy = retrieval_policy
        self.context_builder = context_builder
        self.sufficiency_checker = sufficiency_checker
        self.query_rewriter = query_rewriter
        self.legacy_workflow = legacy_workflow
        self.answer_generator_factory = answer_generator_factory
        self.max_sub_questions = max_sub_questions
        self.sub_question_top_k = sub_question_top_k
        self.max_rewrites = max_rewrites

    def run(self, query: str, top_k: int) -> AgenticAnswerResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")

        plan = self.planner.plan(query)
        if plan.decision_source == "fallback":
            return self._legacy_result(query, top_k, plan)

        sub_questions = plan.sub_questions[: self.max_sub_questions]
        decisions, merged, sub_question_traces, evidence_index = self._fan_out(
            sub_questions
        )
        route = union_route(decisions)
        bundle = self.context_builder.build(query, merged[:top_k])
        sufficiency = self.sufficiency_checker.check_sub_questions(
            query, sub_questions, evidence_index, bundle
        )

        rounds = [
            _round_trace(0, query, route, bundle, sufficiency)
        ]
        retry_count = 0
        stop_reason = _stop_reason(sufficiency, bundle, retry_count, 0)

        while not sufficiency.enough and retry_count < self.max_rewrites and bundle.items:
            try:
                rewrite = self.query_rewriter.rewrite(query, route, sufficiency, bundle)
            except QueryRewriteError as exception:
                rounds[-1].rewrite_error = str(exception)
                stop_reason = "rewrite_failed"
                break

            rounds[-1].rewrite = rewrite
            decision = RouteDecision(
                rewrite.target_query_type,
                DecisionSource.RULES,
                "targeted retry for missing evidence",
            )
            execution = self.retrieval_policy.search_with_trace(
                rewrite.rewritten_query, decision, top_k
            )
            _attribute_retry(
                evidence_index, rewrite.targeted_aspects, execution.results
            )
            merged = _merge_results(execution.results, merged)
            bundle = self.context_builder.build(query, merged[:top_k])
            sufficiency = self.sufficiency_checker.check_sub_questions(
                query, sub_questions, evidence_index, bundle
            )
            retry_count += 1
            rounds.append(
                _round_trace(
                    retry_count,
                    rewrite.rewritten_query,
                    route,
                    bundle,
                    sufficiency,
                )
            )
            if sufficiency.enough:
                stop_reason = "sufficient"
                break
            stop_reason = _stop_reason(
                sufficiency, bundle, retry_count, self.max_rewrites
            )

        answer_result = self._answer(query, bundle, sufficiency, plan)
        trace = AgenticTrace(
            route=route,
            rounds=rounds,
            retry_count=retry_count,
            final_sufficiency=sufficiency,
            stop_reason=stop_reason,
            plan=plan.to_dict(),
            sub_question_traces=sub_question_traces,
            citations=build_citation_trace(answer_result, bundle),
            sections=describe_sections(answer_result.answer),
        )
        return AgenticAnswerResult(answer_result, bundle, trace)

    def _fan_out(
        self, sub_questions: Sequence[SubQuestion]
    ) -> tuple[
        list[RouteDecision],
        list[SearchResult],
        list[SubQuestionTrace],
        dict[str, list[int]],
    ]:
        decisions: list[RouteDecision] = []
        merged: list[SearchResult] = []
        traces: list[SubQuestionTrace] = []
        evidence_index: dict[str, list[int]] = {}
        for sub_question in sub_questions:
            decision = to_route_decision(sub_question)
            execution = self.retrieval_policy.search_with_trace(
                sub_question.question, decision, self.sub_question_top_k
            )
            decisions.append(decision)
            evidence_index[sub_question.id] = [
                result.id for result in execution.results
            ]
            traces.append(
                SubQuestionTrace(
                    sub_question_id=sub_question.id,
                    question=sub_question.question,
                    purpose=sub_question.purpose,
                    query_type=decision.query_type.value,
                    selected_chunks=_selected_chunks(
                        self.context_builder.build(
                            sub_question.question, execution.results
                        )
                    ),
                )
            )
            merged = _merge_results(merged, execution.results)
        return decisions, merged, traces, evidence_index

    def _legacy_result(
        self, query: str, top_k: int, plan: QuestionPlan
    ) -> AgenticAnswerResult:
        result = self.legacy_workflow.run(query, top_k)
        result.trace.plan = plan.to_dict()
        return result

    def _answer(
        self,
        query: str,
        context_bundle: ContextBundle,
        sufficiency,
        plan: QuestionPlan,
    ) -> AnswerResult:
        if not context_bundle.items:
            missing = _format_missing_aspects(sufficiency.missing_aspects)
            suffix = f" 尚缺少：{missing}。" if missing else ""
            return AnswerResult(
                answer=f"{EMPTY_CONTEXT_ANSWER}{suffix}", used_citations=[]
            )

        generator = self.answer_generator_factory()
        outline = render_answer_outline(plan)
        if sufficiency.enough:
            return generator.generate(query, context_bundle, outline=outline)
        missing_aspects = [
            f"[{aspect.source_type}] {aspect.description}"
            for aspect in sufficiency.missing_aspects
        ]
        return generator.generate_partial(
            query, context_bundle, missing_aspects, outline=outline
        )


def _attribute_retry(
    evidence_index: dict[str, list[int]],
    targeted_aspects: Sequence[MissingAspect],
    results: Sequence[SearchResult],
) -> None:
    """Attribute retry results to the sub-questions the rewrite was aimed at.

    The rewriter copies the missing aspects into `targeted_aspects` verbatim, so
    that tuple already names the sub-questions this single rewritten query is for.
    Matching by source type instead would credit same-source sub-questions that the
    rewrite was never aimed at.
    """
    chunk_ids = [result.id for result in results]
    for aspect in targeted_aspects:
        if not aspect.sub_question_id:
            continue
        attributed = evidence_index.setdefault(aspect.sub_question_id, [])
        for chunk_id in chunk_ids:
            if chunk_id not in attributed:
                attributed.append(chunk_id)


def to_route_decision(sub_question: SubQuestion) -> RouteDecision:
    """Translate a sub-question's evidence needs into the route the policy reads."""
    sources = set(sub_question.preferred_sources)
    if sources == {"CODE"}:
        query_type = QueryType.CODE
    elif sources == {"DOCUMENT"}:
        query_type = QueryType.DOC
    else:
        query_type = QueryType.MIXED
    return RouteDecision(
        query_type,
        DecisionSource.RULES,
        f"sub-question {sub_question.id}: {', '.join(sorted(sources))}",
    )


def union_route(decisions: Sequence[RouteDecision]) -> RouteDecision:
    """Collapse sub-question routes into one evidence requirement.

    A sub-question routed MIXED contributes both sources, so ``MIXED`` here means
    "the sub-questions together need code and documentation", not "the user's
    question was classified as MIXED".
    """
    reported = {decision.query_type.value for decision in decisions}
    needs_code = bool(reported & {"CODE", "MIXED"})
    needs_document = bool(reported & {"DOC", "MIXED"})
    if needs_code and needs_document:
        query_type = QueryType.MIXED
    elif needs_code:
        query_type = QueryType.CODE
    elif needs_document:
        query_type = QueryType.DOC
    else:
        query_type = QueryType.MIXED
    reason = (
        "union of sub-question routes: " + ", ".join(sorted(reported))
        if reported
        else "no sub-question routes"
    )
    return RouteDecision(query_type, DecisionSource.RULES, reason)


def render_answer_outline(plan: QuestionPlan) -> str:
    lines = [
        "Answer Outline:",
        f"Intent: {plan.intent_summary}",
        f"Depth: {plan.answer_depth}",
        '请按以下小节组织回答，每节以 "## <标题>" 开头：',
    ]
    lines.extend(
        f"{index}. {sub_question.question}"
        for index, sub_question in enumerate(plan.sub_questions, start=1)
    )
    return "\n".join(lines)


def _selected_chunks(bundle: ContextBundle) -> list[SelectedChunkTrace]:
    return [
        SelectedChunkTrace.from_context_item(item) for item in bundle.items
    ]


def _round_trace(
    round_index: int,
    retrieval_query: str,
    route: RouteDecision,
    bundle: ContextBundle,
    sufficiency,
) -> AgenticRoundTrace:
    return AgenticRoundTrace(
        round_index=round_index,
        retrieval_query=retrieval_query,
        retrieval_query_type=route.query_type,
        selected_chunks=_selected_chunks(bundle),
        sufficiency=sufficiency,
    )


def _stop_reason(
    sufficiency, bundle: ContextBundle, retry_count: int, max_rewrites: int
) -> str:
    if sufficiency.enough:
        return "sufficient"
    if not bundle.items:
        return "empty_context"
    return "retry_limit"
