from __future__ import annotations

from collections.abc import Callable, Sequence

from devcontext.agentic.models import (
    AgenticAnswerResult,
    AgenticRoundTrace,
    AgenticTrace,
    MissingAspect,
    SelectedChunkTrace,
    SufficiencyResult,
    build_citation_trace,
)
from devcontext.agentic.rewrite import QueryRewriteError, TargetedQueryRewriter
from devcontext.agentic.sufficiency import ContextSufficiencyChecker
from devcontext.answer import (
    EMPTY_CONTEXT_ANSWER,
    AnswerGenerator,
    describe_sections,
)
from devcontext.context import ContextBuilder
from devcontext.models import AnswerResult, ContextBundle, SearchResult
from devcontext.retrieval import RetrievalPolicy
from devcontext.routing import (
    DecisionSource,
    QueryRouter,
    QueryType,
    RouteDecision,
)


MAX_RETRIES = 2


class AgenticRetrievalWorkflow:
    def __init__(
        self,
        router: QueryRouter,
        retrieval_policy: RetrievalPolicy,
        context_builder: ContextBuilder,
        sufficiency_checker: ContextSufficiencyChecker,
        query_rewriter: TargetedQueryRewriter,
        answer_generator_factory: Callable[[], AnswerGenerator],
        max_retries: int = MAX_RETRIES,
    ) -> None:
        if max_retries < 0 or max_retries > MAX_RETRIES:
            raise ValueError(f"max_retries must be between 0 and {MAX_RETRIES}")
        self.router = router
        self.retrieval_policy = retrieval_policy
        self.context_builder = context_builder
        self.sufficiency_checker = sufficiency_checker
        self.query_rewriter = query_rewriter
        self.answer_generator_factory = answer_generator_factory
        self.max_retries = max_retries

    def run(self, query: str, top_k: int) -> AgenticAnswerResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")

        route = self.router.route(query)
        retrieval_query = query
        retrieval_query_type = route.query_type
        accumulated_results: list[SearchResult] = []
        rounds: list[AgenticRoundTrace] = []
        seen_queries = {_normalized_query(query)}
        retry_count = 0
        stop_reason = "retry_limit"
        final_bundle = self.context_builder.build(query, [])
        final_sufficiency: SufficiencyResult | None = None

        for round_index in range(self.max_retries + 1):
            round_decision = _retrieval_decision(
                route, retrieval_query_type, round_index
            )
            new_results = self.retrieval_policy.search(
                retrieval_query, round_decision, top_k
            )
            accumulated_results = _merge_results(new_results, accumulated_results)
            final_bundle = self.context_builder.build(query, accumulated_results)
            final_sufficiency = self.sufficiency_checker.check(
                query, route, final_bundle
            )
            round_trace = AgenticRoundTrace(
                round_index=round_index,
                retrieval_query=retrieval_query,
                retrieval_query_type=retrieval_query_type,
                selected_chunks=[
                    SelectedChunkTrace.from_context_item(item)
                    for item in final_bundle.items
                ],
                sufficiency=final_sufficiency,
            )
            rounds.append(round_trace)

            if final_sufficiency.enough:
                stop_reason = "sufficient"
                break
            if round_index == self.max_retries:
                stop_reason = (
                    "empty_context" if not final_bundle.items else "retry_limit"
                )
                break

            try:
                rewrite = self.query_rewriter.rewrite(
                    query, route, final_sufficiency, final_bundle
                )
                normalized_rewrite = _normalized_query(rewrite.rewritten_query)
                if normalized_rewrite in seen_queries:
                    raise QueryRewriteError(
                        "query rewrite repeated a previously executed query"
                    )
            except QueryRewriteError as exception:
                round_trace.rewrite_error = str(exception)
                stop_reason = (
                    "empty_context" if not final_bundle.items else "rewrite_failed"
                )
                break

            round_trace.rewrite = rewrite
            seen_queries.add(normalized_rewrite)
            retrieval_query = rewrite.rewritten_query
            retrieval_query_type = rewrite.target_query_type
            retry_count += 1

        assert final_sufficiency is not None
        answer_result = self._answer(query, final_bundle, final_sufficiency)
        trace = AgenticTrace(
            route=route,
            rounds=rounds,
            retry_count=retry_count,
            final_sufficiency=final_sufficiency,
            stop_reason=stop_reason,
            citations=build_citation_trace(answer_result, final_bundle),
            sections=describe_sections(answer_result.answer),
        )
        return AgenticAnswerResult(answer_result, final_bundle, trace)

    def _answer(
        self,
        query: str,
        context_bundle: ContextBundle,
        sufficiency: SufficiencyResult,
    ) -> AnswerResult:
        if not context_bundle.items:
            missing = _format_missing_aspects(sufficiency.missing_aspects)
            suffix = f" 尚缺少：{missing}。" if missing else ""
            return AnswerResult(
                answer=f"{EMPTY_CONTEXT_ANSWER}{suffix}", used_citations=[]
            )

        generator = self.answer_generator_factory()
        if sufficiency.enough:
            return generator.generate(query, context_bundle)
        missing_aspects = [
            f"[{aspect.source_type}] {aspect.description}"
            for aspect in sufficiency.missing_aspects
        ]
        return generator.generate_partial(
            query, context_bundle, missing_aspects
        )


def _retrieval_decision(
    original_route: RouteDecision,
    query_type: QueryType,
    round_index: int,
) -> RouteDecision:
    if round_index == 0:
        return original_route
    return RouteDecision(
        query_type=query_type,
        decision_source=DecisionSource.RULES,
        reason="targeted retry for missing evidence",
    )


def _merge_results(
    new_results: Sequence[SearchResult],
    previous_results: Sequence[SearchResult],
) -> list[SearchResult]:
    merged: list[SearchResult] = []
    seen: set[int] = set()
    for result in (*new_results, *previous_results):
        if result.id in seen:
            continue
        seen.add(result.id)
        merged.append(result)
    return merged


def _normalized_query(query: str) -> str:
    return " ".join(query.split()).casefold()


def _format_missing_aspects(aspects: Sequence[MissingAspect]) -> str:
    return "；".join(
        f"{aspect.source_type}：{aspect.description}" for aspect in aspects
    )
