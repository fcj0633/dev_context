from __future__ import annotations

import re
import time
from collections.abc import Callable

from devcontext.agentic.evidence_models import (
    EvidencePackage,
    build_requirement_traces,
)
from devcontext.agentic.models import (
    AgenticAnswerResult,
    AgenticRoundTrace,
    AgenticTrace,
    SelectedChunkTrace,
    StageUsage,
    SubQuestionTrace,
    build_citation_trace,
)
from devcontext.agentic.retrieval_controller import RetrievalController
from devcontext.answer import (
    EMPTY_CONTEXT_ANSWER,
    AnswerGenerator,
    AnswerPlanner,
    AnswerReviewer,
    GroundedDraft,
    ReviewResult,
    describe_sections,
    extract_citations,
    fallback_evidence_answer_plan,
)
from devcontext.models import AnswerResult
from devcontext.request import AnswerOptions, UserRequest
from devcontext.routing import DecisionSource, QueryType, RouteDecision


class EvidenceDrivenWorkflow:
    """Orchestrate retrieval and answering across the EvidencePackage boundary."""

    def __init__(
        self,
        retrieval_controller: RetrievalController,
        answer_generator_factory: Callable[[], AnswerGenerator],
        answer_planner: AnswerPlanner | None = None,
        answer_reviewer: AnswerReviewer | None = None,
        *,
        answer_mode: str = "legacy",
        depth_override: str | None = None,
        context_budget_override: int | None = None,
    ) -> None:
        self.retrieval_controller = retrieval_controller
        self.answer_generator_factory = answer_generator_factory
        self.answer_planner = answer_planner
        self.answer_reviewer = answer_reviewer
        self.answer_options = AnswerOptions(depth_override, answer_mode)
        if context_budget_override is not None and context_budget_override < 1:
            raise ValueError("context_budget_override must be positive")
        self.context_budget_override = context_budget_override

    def run(self, query: str, top_k: int) -> AgenticAnswerResult:
        request = UserRequest(
            query,
            self.answer_options,
            self.context_budget_override,
        )
        retrieval = self.retrieval_controller.retrieve(request, top_k)
        package = retrieval.package
        answer_result, answer_plan, review, answer_stages = self._answer(
            request, package
        )
        sufficiency = package.to_legacy_sufficiency()
        route = _route_for_package(package)
        selected = [
            SelectedChunkTrace.from_context_item(item)
            for item in package.context_bundle.items
        ]
        rounds = []
        for round_value in package.coverage_rounds:
            queries = [
                item.query
                for item in package.search_history
                if item.round_index == round_value.round_index
            ]
            rounds.append(
                AgenticRoundTrace(
                    round_value.round_index,
                    " | ".join(queries),
                    route.query_type,
                    selected,
                    sufficiency,
                )
            )
        requirement_traces = build_requirement_traces(package)
        legacy_plan = _legacy_plan_alias(request, package, answer_plan)
        legacy_sub_question_traces = _legacy_requirement_trace_aliases(package)
        trace = AgenticTrace(
            route=route,
            rounds=rounds,
            retry_count=max(0, len(package.coverage_rounds) - 1),
            final_sufficiency=sufficiency,
            stop_reason=package.retrieval_state.lower(),
            # One-cycle aliases for existing debug/evaluation consumers.
            plan=legacy_plan,
            sub_question_traces=legacy_sub_question_traces,
            citations=build_citation_trace(answer_result, package.context_bundle),
            sections=describe_sections(answer_result.answer),
            answer_plan=answer_plan.to_dict() if answer_plan else None,
            source_conflicts=(
                [item.to_dict() for item in answer_plan.conflicts]
                if answer_plan else []
            ),
            review=review.to_dict() if review else None,
            stage_usage=[*retrieval.stage_usage, *answer_stages],
            evidence_plan=package.evidence_plan.to_dict(),
            requirement_traces=[item.to_dict() for item in requirement_traces],
            search_actions=[item.to_dict() for item in package.search_history],
            coverage_rounds=[item.to_dict() for item in package.coverage_rounds],
            final_coverage=[
                item.to_dict() for item in package.requirement_coverage
            ],
            evidence_package_state=package.retrieval_state,
        )
        return AgenticAnswerResult(answer_result, package.context_bundle, trace)

    def _answer(
        self,
        request: UserRequest,
        package: EvidencePackage,
    ):
        if package.retrieval_state == "EMPTY" or not package.context_bundle.items:
            unresolved_ids = set(package.unresolved_requirements)
            unresolved = "；".join(
                item.target
                for item in package.evidence_plan.requirements
                if item.id in unresolved_ids
            )
            suffix = f" 当前仍缺少：{unresolved}。" if unresolved else ""
            return (
                AnswerResult(EMPTY_CONTEXT_ANSWER + suffix, []),
                None,
                None,
                [],
            )
        generator = self.answer_generator_factory()
        if request.answer_options.answer_mode == "legacy":
            started = time.perf_counter()
            sufficiency = package.to_legacy_sufficiency()
            if sufficiency.enough:
                result = generator.generate(
                    request.original_query, package.context_bundle
                )
            else:
                gaps = [
                    item.description for item in sufficiency.missing_aspects
                ] or ["部分核心证据需求尚未满足"]
                result = generator.generate_partial(
                    request.original_query,
                    package.context_bundle,
                    gaps,
                )
            return (
                result,
                None,
                None,
                [
                    StageUsage(
                        "answer_generation",
                        (time.perf_counter() - started) * 1000,
                        "legacy",
                    )
                ],
            )
        return self._answer_explain(request, package, generator)

    def _answer_explain(
        self,
        request: UserRequest,
        package: EvidencePackage,
        generator: AnswerGenerator,
    ):
        stages: list[StageUsage] = []
        started = time.perf_counter()
        try:
            if self.answer_planner is None:
                raise RuntimeError("answer planner is unavailable")
            answer_plan = self.answer_planner.plan_evidence(request, package)
        except Exception:
            answer_plan = fallback_evidence_answer_plan(request, package)
        stages.append(
            _stage_usage(
                "answer_planning",
                started,
                answer_plan.decision_source,
                getattr(self.answer_planner, "last_client", None),
                "high",
            )
        )

        started = time.perf_counter()
        draft = generator.generate_explained_draft(
            request.original_query,
            package.context_bundle,
            answer_plan,
        )
        stages.append(
            _stage_usage(
                "grounded_draft",
                started,
                "llm",
                getattr(generator, "client", None),
                "high",
            )
        )
        review = None
        should_review = answer_plan.answer_depth == "detailed" or (
            answer_plan.answer_depth == "standard"
            and (
                bool(answer_plan.conflicts)
                or not draft.used_citations
                or bool(draft.invalid_citations)
                or answer_plan.decision_source == "fallback"
                or _draft_structure_failed(draft, answer_plan.answer_depth)
            )
        )
        if should_review and self.answer_reviewer is not None:
            started = time.perf_counter()
            try:
                review = self.answer_reviewer.review_evidence(
                    request, package, answer_plan, draft
                )
                labels = extract_citations(review.final_answer_with_citations)
                allowed = {
                    item.citation.label for item in package.context_bundle.items
                }
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
                    type(exception).__name__,
                )
            stages.append(
                _stage_usage(
                    "answer_review",
                    started,
                    review.decision_source,
                    getattr(self.answer_reviewer, "last_client", None),
                    "high",
                )
            )
        return generator.finalize_draft(draft), answer_plan, review, stages


def _route_for_package(package: EvidencePackage) -> RouteDecision:
    scopes = {
        item.source_requirement for item in package.evidence_plan.requirements
    }
    needs_code = bool(scopes & {"CODE", "BOTH", "ANY"})
    needs_document = bool(scopes & {"DOCUMENT", "BOTH", "ANY"})
    if needs_code and needs_document:
        query_type = QueryType.MIXED
    elif needs_document:
        query_type = QueryType.DOC
    else:
        query_type = QueryType.CODE
    return RouteDecision(
        query_type,
        DecisionSource.RULES,
        "union of evidence source requirements",
    )


def _legacy_plan_alias(
    request: UserRequest,
    package: EvidencePackage,
    answer_plan: object | None,
) -> dict[str, object]:
    """Serialize the deprecated QuestionPlan shape without using it internally."""
    depth = getattr(answer_plan, "answer_depth", None) or (
        request.answer_options.depth_override or "standard"
    )
    goal = getattr(answer_plan, "answer_goal", None) or request.original_query
    strategy = getattr(answer_plan, "explanation_strategy", None) or "mixed"
    return {
        "original_query": request.original_query,
        "intent_summary": request.original_query,
        "answer_depth": depth,
        "decision_source": package.evidence_plan.decision_source,
        "answer_goal": goal,
        "explanation_strategy": strategy,
        "sub_questions": [
            {
                "id": item.id,
                "question": item.target,
                "purpose": item.success_criteria,
                "evidence_description": item.success_criteria,
                "preferred_sources": _legacy_sources(item.source_requirement),
                "retrieval_query": "",
                "importance": item.priority,
                "temporal_scope": item.temporal_scope,
            }
            for item in package.evidence_plan.requirements
        ],
    }


def _legacy_requirement_trace_aliases(
    package: EvidencePackage,
) -> list[SubQuestionTrace]:
    return [
        SubQuestionTrace(
            item.id,
            item.target,
            item.success_criteria,
            _legacy_query_type(item.source_requirement),
            [
                SelectedChunkTrace.from_context_item(context_item)
                for context_item in package.context_bundle.items
                if item.id in context_item.sub_question_ids
            ],
        )
        for item in package.evidence_plan.requirements
    ]


def _legacy_sources(source_requirement: str) -> list[str]:
    if source_requirement == "CODE":
        return ["CODE"]
    if source_requirement == "DOCUMENT":
        return ["DOCUMENT"]
    return ["CODE", "DOCUMENT"]


def _legacy_query_type(source_requirement: str) -> str:
    if source_requirement == "CODE":
        return "CODE"
    if source_requirement == "DOCUMENT":
        return "DOC"
    return "MIXED"


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


def _stage_usage(
    stage: str,
    started: float,
    decision_source: str,
    client: object | None,
    default_effort: str,
) -> StageUsage:
    usage = getattr(client, "last_usage", {}) if client is not None else {}
    if not isinstance(usage, dict):
        usage = {}
    return StageUsage(
        stage,
        (time.perf_counter() - started) * 1000,
        decision_source,
        getattr(client, "model", None),
        getattr(client, "reasoning_effort", default_effort),
        usage.get("prompt_tokens", usage.get("input_tokens")),
        usage.get("completion_tokens", usage.get("output_tokens")),
    )
