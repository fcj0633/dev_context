from __future__ import annotations

import time
import re
from collections.abc import Callable, Sequence
from dataclasses import replace

from devcontext.agentic.models import (
    AgenticAnswerResult,
    AgenticRoundTrace,
    AgenticTrace,
    MissingAspect,
    RewriteResult,
    SelectedChunkTrace,
    SubQuestionTrace,
    StageUsage,
    SufficiencyResult,
    build_citation_trace,
)
from devcontext.agentic.rewrite import QueryRewriteError, TargetedQueryRewriter
from devcontext.agentic.sufficiency import ContextSufficiencyChecker
from devcontext.agentic.workflow import (
    AgenticRetrievalWorkflow,
    _format_missing_aspects,
)
from devcontext.answer import (
    EMPTY_CONTEXT_ANSWER,
    AnswerPlan,
    AnswerPlanner,
    AnswerReviewer,
    AnswerGenerator,
    describe_sections,
    extract_citations,
    fallback_answer_plan,
    GroundedDraft,
    ReviewResult,
)
from devcontext.context import ContextBuilder
from devcontext.models import AnswerResult, ContextBundle, SearchResult
from devcontext.evidence import (
    EvidencePool,
    SourcePolicy,
    default_source_policy_path,
)
from devcontext.planning import (
    ANSWER_DEPTHS,
    MAX_SUB_QUESTIONS,
    QuestionPlan,
    QuestionPlanner,
    SubQuestion,
)
from devcontext.retrieval import RetrievalPolicy
from devcontext.routing import DecisionSource, QueryType, RouteDecision


SUB_QUESTION_TOP_K = 3
CORE_TOP_K = 5
SUPPORTING_TOP_K = 3
MAX_RETRY_TARGETS = 4
DEPTH_CONTEXT_BUDGETS = {"brief": 8000, "standard": 16000, "detailed": 28000}
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
        answer_planner: AnswerPlanner | None = None,
        answer_reviewer: AnswerReviewer | None = None,
        source_policy: SourcePolicy | None = None,
        answer_mode: str = "legacy",
        context_budget_override: int | None = None,
        depth_override: str | None = None,
    ) -> None:
        if not 1 <= max_sub_questions <= MAX_SUB_QUESTIONS:
            raise ValueError(
                f"max_sub_questions must be between 1 and {MAX_SUB_QUESTIONS}"
            )
        if sub_question_top_k < 1:
            raise ValueError("sub_question_top_k must be positive")
        if max_rewrites < 0:
            raise ValueError("max_rewrites must not be negative")
        if answer_mode not in {"legacy", "explain"}:
            raise ValueError("answer_mode must be legacy or explain")
        if depth_override is not None and depth_override not in ANSWER_DEPTHS:
            raise ValueError("depth_override must be brief, standard, or detailed")
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
        self.answer_planner = answer_planner
        self.answer_reviewer = answer_reviewer
        self.source_policy = source_policy or SourcePolicy.from_file(
            default_source_policy_path()
        )
        self.answer_mode = answer_mode
        self.context_budget_override = context_budget_override
        self.depth_override = depth_override

    def run(self, query: str, top_k: int) -> AgenticAnswerResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")

        stage_usage: list[StageUsage] = []
        started = time.perf_counter()
        plan = self.planner.plan(query)
        if self.depth_override is not None:
            plan = replace(plan, answer_depth=self.depth_override)
        stage_usage.append(_stage_usage(
            "investigation_planning",
            (time.perf_counter() - started) * 1000,
            plan.decision_source,
            getattr(self.planner, "last_client", None),
            "high",
        ))
        if plan.decision_source == "fallback":
            return self._legacy_result(query, top_k, plan, stage_usage)

        sub_questions = plan.sub_questions[: self.max_sub_questions]
        started = time.perf_counter()
        decisions, pool, sub_question_traces, evidence_index = self._fan_out(
            sub_questions
        )
        route = union_route(decisions)
        bundle = self._build_context(query, plan, sub_questions, pool, top_k)
        stage_usage.append(StageUsage(
            "evidence_retrieval",
            (time.perf_counter() - started) * 1000,
            "policy",
        ))
        started = time.perf_counter()
        sufficiency = self.sufficiency_checker.check_sub_questions(
            query, sub_questions, evidence_index, bundle
        )
        stage_usage.append(_stage_usage(
            "sufficiency",
            (time.perf_counter() - started) * 1000,
            sufficiency.decision_source,
            getattr(self.sufficiency_checker, "last_requirement_client", None),
            "low",
        ))

        rounds = [
            _round_trace(0, query, route, bundle, sufficiency)
        ]
        retry_count = 0
        stop_reason = _stop_reason(sufficiency, bundle, retry_count, 0)

        while not sufficiency.enough and retry_count < self.max_rewrites and bundle.items:
            retry_started = time.perf_counter()
            rewrites = []
            rewrite_errors: list[str] = []
            for group_index, focused_sufficiency in enumerate(
                _retry_groups(sufficiency)
            ):
                if group_index == 0:
                    try:
                        rewrite = self.query_rewriter.rewrite(
                            query, route, focused_sufficiency, bundle
                        )
                    except QueryRewriteError as exception:
                        rewrite_errors.append(str(exception))
                        continue
                else:
                    rewrite = _deterministic_retry(
                        query, focused_sufficiency, sub_questions, bundle
                    )

                rewrites.append(rewrite)
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
                targets = {
                    aspect.sub_question_id
                    for aspect in rewrite.targeted_aspects
                    if aspect.sub_question_id
                }
                for target in targets:
                    pool.add_many(
                        self.source_policy.classify(result, target)
                        for result in execution.results
                    )

            rounds[-1].rewrites = rewrites
            rounds[-1].rewrite = rewrites[0] if rewrites else None
            if rewrite_errors:
                rounds[-1].rewrite_error = "; ".join(rewrite_errors)
            if not rewrites:
                stop_reason = "rewrite_failed"
                stage_usage.append(_stage_usage(
                    "targeted_retry",
                    (time.perf_counter() - retry_started) * 1000,
                    "fallback",
                    getattr(self.query_rewriter, "last_client", None),
                    "low",
                ))
                break
            bundle = self._build_context(query, plan, sub_questions, pool, top_k)
            sufficiency = self.sufficiency_checker.check_sub_questions(
                query, sub_questions, evidence_index, bundle
            )
            retry_count += 1
            rounds.append(
                _round_trace(
                    retry_count,
                    " | ".join(item.rewritten_query for item in rewrites),
                    route,
                    bundle,
                    sufficiency,
                )
            )
            stage_usage.append(_stage_usage(
                "targeted_retry",
                (time.perf_counter() - retry_started) * 1000,
                sufficiency.decision_source,
                getattr(self.query_rewriter, "last_client", None),
                "low",
            ))
            if sufficiency.enough:
                stop_reason = "sufficient"
                break
            stop_reason = _stop_reason(
                sufficiency, bundle, retry_count, self.max_rewrites
            )

        answer_started = time.perf_counter()
        answer_result, answer_plan, review, answer_stages = self._answer(
            query, bundle, sufficiency, plan
        )
        if answer_stages:
            stage_usage.extend(answer_stages)
        else:
            stage_usage.append(StageUsage(
                "answer_generation",
                (time.perf_counter() - answer_started) * 1000,
                "legacy",
            ))
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
            answer_plan=answer_plan.to_dict() if answer_plan else None,
            source_conflicts=(
                [conflict.to_dict() for conflict in answer_plan.conflicts]
                if answer_plan else []
            ),
            review=review.to_dict() if review else None,
            stage_usage=stage_usage,
        )
        return AgenticAnswerResult(answer_result, bundle, trace)

    def _fan_out(
        self, sub_questions: Sequence[SubQuestion]
    ) -> tuple[
        list[RouteDecision],
        EvidencePool,
        list[SubQuestionTrace],
        dict[str, list[int]],
    ]:
        decisions: list[RouteDecision] = []
        pool = EvidencePool()
        traces: list[SubQuestionTrace] = []
        evidence_index: dict[str, list[int]] = {}
        for sub_question in sub_questions:
            decision = to_route_decision(sub_question)
            retrieval_query = sub_question.retrieval_query.strip() or (
                f"{sub_question.question} {sub_question.evidence_description}"
            )
            per_question_top_k = (
                CORE_TOP_K if sub_question.importance == "CORE" else SUPPORTING_TOP_K
            )
            execution = self.retrieval_policy.search_with_trace(
                retrieval_query, decision, per_question_top_k
            )
            pool.add_many(
                self.source_policy.classify(result, sub_question.id)
                for result in execution.results
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
                            retrieval_query, execution.results
                        )
                    ),
                )
            )
        return decisions, pool, traces, evidence_index

    def _build_context(
        self,
        query: str,
        plan: QuestionPlan,
        sub_questions: Sequence[SubQuestion],
        pool: EvidencePool,
        top_k: int,
    ) -> ContextBundle:
        results, annotations = pool.select(sub_questions, top_k)
        budget = self.context_budget_override or DEPTH_CONTEXT_BUDGETS[plan.answer_depth]
        builder = ContextBuilder(max_chars=budget)
        return builder.build(query, results, annotations)

    def _legacy_result(
        self,
        query: str,
        top_k: int,
        plan: QuestionPlan,
        stage_usage: list[StageUsage],
    ) -> AgenticAnswerResult:
        result = self.legacy_workflow.run(query, top_k)
        result.trace.plan = plan.to_dict()
        result.trace.stage_usage = stage_usage + result.trace.stage_usage
        return result

    def _answer(
        self,
        query: str,
        context_bundle: ContextBundle,
        sufficiency,
        plan: QuestionPlan,
    ) -> tuple[AnswerResult, AnswerPlan | None, ReviewResult | None, list[StageUsage]]:
        if not context_bundle.items:
            missing = _format_missing_aspects(sufficiency.missing_aspects)
            suffix = f" 尚缺少：{missing}。" if missing else ""
            return AnswerResult(
                answer=f"{EMPTY_CONTEXT_ANSWER}{suffix}", used_citations=[]
            ), None, None, []

        generator = self.answer_generator_factory()
        if self.answer_mode == "explain":
            return self._answer_explain(
                query, context_bundle, sufficiency, plan, generator
            )
        outline = render_answer_outline(plan)
        if sufficiency.enough:
            return generator.generate(query, context_bundle, outline=outline), None, None, []
        missing_aspects = [
            f"[{aspect.source_type}] {aspect.description}"
            for aspect in sufficiency.missing_aspects
        ]
        return generator.generate_partial(
            query, context_bundle, missing_aspects, outline=outline
        ), None, None, []

    def _answer_explain(
        self,
        query: str,
        context_bundle: ContextBundle,
        sufficiency,
        plan: QuestionPlan,
        generator: AnswerGenerator,
    ) -> tuple[AnswerResult, AnswerPlan, ReviewResult | None, list[StageUsage]]:
        stages: list[StageUsage] = []
        gaps = tuple(
            f"[{aspect.source_type}] {aspect.description}"
            for aspect in sufficiency.missing_aspects
        )
        answer_plan: AnswerPlan
        if plan.answer_depth != "brief" and self.answer_planner is not None:
            started = time.perf_counter()
            try:
                answer_plan = self.answer_planner.plan(
                    query, plan, context_bundle, gaps
                )
            except Exception:
                answer_plan = fallback_answer_plan(plan, context_bundle, gaps)
            stages.append(_stage_usage(
                "answer_planning",
                (time.perf_counter() - started) * 1000,
                answer_plan.decision_source,
                getattr(self.answer_planner, "last_client", None),
                "high",
            ))
        else:
            answer_plan = fallback_answer_plan(plan, context_bundle, gaps)

        started = time.perf_counter()
        draft = generator.generate_explained_draft(
            query, context_bundle, answer_plan
        )
        stages.append(_stage_usage(
            "grounded_draft",
            (time.perf_counter() - started) * 1000,
            "llm",
            getattr(generator, "client", None),
            "high",
        ))
        review = None
        should_review = plan.answer_depth == "detailed" or (
            plan.answer_depth == "standard"
            and (
                bool(answer_plan.conflicts)
                or not draft.used_citations
                or bool(draft.invalid_citations)
                or answer_plan.decision_source == "fallback"
                or not answer_plan.direct_answer.strip()
                or _draft_structure_failed(draft, plan.answer_depth)
            )
        )
        if should_review and self.answer_reviewer is not None:
            started = time.perf_counter()
            try:
                review = self.answer_reviewer.review(
                    query, plan, answer_plan, draft, context_bundle
                )
                labels = extract_citations(review.final_answer_with_citations)
                allowed = {item.citation.label for item in context_bundle.items}
                draft = GroundedDraft(
                    review.final_answer_with_citations,
                    tuple(label for label in labels if label in allowed),
                    tuple(label for label in labels if label not in allowed),
                )
            except Exception as exception:
                review = ReviewResult(
                    False,
                    (),
                    draft.text_with_citations,
                    "fallback",
                    f"{type(exception).__name__}: {exception}",
                )
            stages.append(_stage_usage(
                "answer_review",
                (time.perf_counter() - started) * 1000,
                review.decision_source if review else "fallback",
                getattr(self.answer_reviewer, "last_client", None),
                "high",
            ))
        return generator.finalize_draft(draft), answer_plan, review, stages


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


def _retry_groups(sufficiency: SufficiencyResult) -> list[SufficiencyResult]:
    """Create up to four independent retry tasks, one per named requirement."""
    grouped: list[list[MissingAspect]] = []
    group_indexes: dict[str, int] = {}
    for index, aspect in enumerate(sufficiency.missing_aspects):
        key = aspect.sub_question_id or f"__unattributed_{index}"
        if key not in group_indexes:
            if len(grouped) >= MAX_RETRY_TARGETS:
                break
            group_indexes[key] = len(grouped)
            grouped.append([])
        grouped[group_indexes[key]].append(aspect)
    return [
        SufficiencyResult(
            enough=False,
            missing_aspects=tuple(aspects),
            reason=sufficiency.reason,
            decision_source=sufficiency.decision_source,
            statuses=sufficiency.statuses,
        )
        for aspects in grouped
    ]


def _deterministic_retry(
    original_query: str,
    sufficiency: SufficiencyResult,
    sub_questions: Sequence[SubQuestion],
    context_bundle: ContextBundle,
) -> RewriteResult:
    by_id = {item.id: item for item in sub_questions}
    target_id = sufficiency.missing_aspects[0].sub_question_id
    sub_question = by_id.get(target_id)
    base = (
        sub_question.retrieval_query
        if sub_question and sub_question.retrieval_query.strip()
        else sub_question.question if sub_question else original_query
    )
    gaps = " ".join(
        aspect.description for aspect in sufficiency.missing_aspects
    )
    discovered: list[str] = []
    for item in context_bundle.items:
        if target_id and target_id not in item.sub_question_ids:
            continue
        citation = item.citation
        identity = (
            citation.signature
            or citation.symbol_name
            or citation.class_name
            or " ".join(citation.heading_path)
        )
        if identity and identity not in discovered:
            discovered.append(identity)
        if len(discovered) >= 5:
            break
    discovered_terms = " ".join(discovered)
    rewritten_query = " ".join(
        f"{base} {gaps} {discovered_terms}".split()
    )[:500]
    sources = {aspect.source_type for aspect in sufficiency.missing_aspects}
    if sources == {"CODE"}:
        query_type = QueryType.CODE
    elif sources == {"DOCUMENT"}:
        query_type = QueryType.DOC
    else:
        query_type = QueryType.MIXED
    return RewriteResult(
        original_query,
        rewritten_query,
        query_type,
        sufficiency.missing_aspects,
    )


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


def _stage_usage(
    stage: str,
    latency_ms: float,
    decision_source: str,
    client: object | None,
    default_effort: str,
) -> StageUsage:
    usage = getattr(client, "last_usage", {}) if client is not None else {}
    if not isinstance(usage, dict):
        usage = {}
    return StageUsage(
        stage=stage,
        latency_ms=latency_ms,
        decision_source=decision_source,
        model=getattr(client, "model", None),
        reasoning_effort=getattr(client, "reasoning_effort", default_effort),
        input_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
        output_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
    )


def _draft_structure_failed(draft: GroundedDraft, depth: str) -> bool:
    if depth == "brief":
        return False
    text = draft.text_with_citations
    headings = [
        " ".join(value.split()).casefold()
        for value in re.findall(r"(?m)^#{1,6}\s+(.+)$", text)
    ]
    if len(headings) != len(set(headings)) or text.count("结论：") > 1:
        return True
    chinese_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    minimum, maximum = {
        "standard": (800, 1800),
        "detailed": (2200, 5000),
    }[depth]
    return not minimum <= chinese_chars <= maximum
