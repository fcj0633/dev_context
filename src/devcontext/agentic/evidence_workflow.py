from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
from devcontext.agentic.retrieval_engine import RetrievalEngine
from devcontext.answer import (
    EMPTY_CONTEXT_ANSWER,
    RETRIEVAL_FAILED_ANSWER,
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

if TYPE_CHECKING:
    from devcontext.explanation.models import ExplanationPlan
    from devcontext.explanation.workflow import TeachingExplanationWorkflow


@dataclass(slots=True)
class _AnswerOutcome:
    """What the answer stage produced, without growing the return tuple."""

    answer_result: AnswerResult
    answer_plan: Any | None = None
    review: Any | None = None
    stages: list[StageUsage] = field(default_factory=list)
    explanation_plan: "ExplanationPlan | None" = None
    # Only the teach path replaces the retrieval bundle; everything else leaves
    # this None and the retrieval context is used unchanged.
    context_bundle: Any | None = None
    evidence_catalog: Any | None = None
    workspace_stats: Mapping[str, Any] | None = None


class EvidenceDrivenWorkflow:
    """Orchestrate retrieval and answering across the EvidencePackage boundary."""

    def __init__(
        self,
        retrieval_controller: RetrievalEngine,
        answer_generator_factory: Callable[[], AnswerGenerator],
        answer_planner: AnswerPlanner | None = None,
        answer_reviewer: AnswerReviewer | None = None,
        *,
        answer_mode: str = "legacy",
        context_budget_override: int | None = None,
        teaching_workflow: "TeachingExplanationWorkflow | None" = None,
        request_policy=None,
    ) -> None:
        self.retrieval_controller = retrieval_controller
        self.answer_generator_factory = answer_generator_factory
        self.answer_planner = answer_planner
        self.answer_reviewer = answer_reviewer
        self.teaching_workflow = teaching_workflow
        self.request_policy = request_policy
        self.answer_options = AnswerOptions(answer_mode=answer_mode)
        if context_budget_override is not None and context_budget_override < 1:
            raise ValueError("context_budget_override must be positive")
        self.context_budget_override = context_budget_override

    def run(self, query: str, top_k: int, *, on_section=None) -> AgenticAnswerResult:
        runtime = getattr(self.teaching_workflow, "runtime_options", None)
        if (self.request_policy and self.request_policy.profile == "full") or getattr(runtime, "generation_mode", None) == "v3":
            from devcontext.llm.errors import fatal_request_scope
            with fatal_request_scope():
                return self._run_with_deadline(query, top_k, on_section=on_section)
        return self._run_with_deadline(query, top_k, on_section=on_section)

    def _run_with_deadline(self, query: str, top_k: int, *, on_section=None) -> AgenticAnswerResult:
        self.on_section = on_section
        if self.request_policy is not None:
            from devcontext.deadline import request_deadline
            with request_deadline(self.request_policy.hard_timeout_seconds):
                return self._run(query, top_k)
        runtime = getattr(self.teaching_workflow, "runtime_options", None)
        if getattr(runtime, "generation_mode", None) == "v3":
            from devcontext.deadline import request_deadline
            with request_deadline(getattr(self.teaching_workflow, "request_timeout_seconds", 180)):
                return self._run(query, top_k)
        return self._run(query, top_k)

    def _run(self, query: str, top_k: int) -> AgenticAnswerResult:
        policy_started = time.perf_counter()
        if self.teaching_workflow is not None:
            self.teaching_workflow.request_started_at = time.perf_counter()
        request = UserRequest(
            query,
            self.answer_options,
            self.context_budget_override,
            self.request_policy,
        )
        policy_ms = (time.perf_counter() - policy_started) * 1000
        retrieval = self.retrieval_controller.retrieve(request, top_k)
        intent = getattr(getattr(self.retrieval_controller, "evidence_planner", None), "primary_intent", None)
        if request.policy and intent:
            from dataclasses import replace
            request = replace(request, policy=replace(request.policy, primary_intent=intent))
        package = retrieval.package
        outcome = self._answer(request, package)
        if request.policy is not None:
            trace_data = (outcome.workspace_stats or {}).get("trace", {})
            trace_data.update(request_policy=request.policy.to_dict(), profile=request.policy.profile,
                reasoning_requested=request.policy.reasoning_effort)
            elapsed = (time.perf_counter() - self.teaching_workflow.request_started_at) * 1000
            trace_data.update(total_elapsed_ms=elapsed,
                latency_target_exceeded=elapsed > request.policy.latency_target_seconds * 1000)
        runtime = getattr(self.teaching_workflow, "runtime_options", None)
        if getattr(runtime, "generation_mode", None) == "v3" and not (outcome.workspace_stats or {}).get("trace"):
            # Empty/failed retrieval is also a failed V3 request, not CLI success.
            outcome.workspace_stats = {"trace": {
                "generation_mode": "v3", "completion_status": "failed", "sections_emitted": 0,
                "error": outcome.answer_result.answer, "stream_partial": False,
            }}
        answer_result = outcome.answer_result
        answer_plan = outcome.answer_plan
        review = outcome.review
        answer_stages = outcome.stages
        final_bundle = outcome.context_bundle or package.context_bundle
        sufficiency = package.to_legacy_sufficiency()
        route = _route_for_package(package)
        selected = [
            SelectedChunkTrace.from_context_item(item)
            for item in final_bundle.items
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
        # Only the teach path has no AnswerPlan to alias from; the legacy, explain
        # and empty-evidence paths all keep generating it as before.
        legacy_plan = (
            None
            if outcome.explanation_plan is not None
            else _legacy_plan_alias(request, package, answer_plan)
        )
        legacy_sub_question_traces = _legacy_requirement_trace_aliases(package)
        conflicts = (
            answer_plan.conflicts
            if answer_plan is not None
            else getattr(outcome.explanation_plan, "conflicts", ())
        )
        trace = AgenticTrace(
            route=route,
            rounds=rounds,
            retry_count=max(0, len(package.coverage_rounds) - 1),
            final_sufficiency=sufficiency,
            stop_reason=package.retrieval_state.lower(),
            # One-cycle aliases for existing debug/evaluation consumers.
            plan=legacy_plan,
            sub_question_traces=legacy_sub_question_traces,
            citations=build_citation_trace(answer_result, final_bundle),
            sections=describe_sections(answer_result.answer),
            answer_plan=answer_plan.to_dict() if answer_plan else None,
            source_conflicts=[item.to_dict() for item in conflicts],
            review=review.to_dict() if review else None,
            stage_usage=[*([StageUsage("request_policy", policy_ms, "rules")] if request.policy else []), *retrieval.stage_usage, *answer_stages],
            evidence_plan=package.evidence_plan.to_dict(),
            requirement_traces=[item.to_dict() for item in requirement_traces],
            search_actions=[item.to_dict() for item in package.search_history],
            coverage_rounds=[item.to_dict() for item in package.coverage_rounds],
            final_coverage=[
                item.to_dict() for item in package.requirement_coverage
            ],
            evidence_package_state=package.retrieval_state,
            explanation_plan=(
                outcome.explanation_plan.to_dict()
                if outcome.explanation_plan is not None
                else None
            ),
            teaching=(outcome.workspace_stats or {}).get("trace"),
            agent_retrieval=retrieval.agent_trace,
        )
        return AgenticAnswerResult(
            answer_result,
            final_bundle,
            trace,
            outcome.evidence_catalog or package.evidence_catalog,
            outcome.workspace_stats,
        )

    def _answer(
        self,
        request: UserRequest,
        package: EvidencePackage,
    ):
        from devcontext.deadline import remaining_seconds, RequestDeadlineExceeded
        try:
            remaining_seconds()
        except RequestDeadlineExceeded as exc:
            return _AnswerOutcome(AnswerResult(str(exc), []), workspace_stats={"trace": {
                "generation_mode": "single_stream" if request.policy and request.policy.profile == "fast" else "v3",
                "completion_status": "failed", "error": str(exc), "sections_emitted": 0}})
        if package.retrieval_state == "RETRIEVAL_FAILED":
            # Distinct from "no evidence found": the search never completed, so
            # saying the project lacks this evidence would be a false statement.
            detail = next(
                (item.error for item in package.search_history if item.error), None
            )
            suffix = f" 最后一次失败：{detail}。" if detail else ""
            if request.policy is None:
                return _AnswerOutcome(AnswerResult(RETRIEVAL_FAILED_ANSWER + suffix, []))
        workspace = package.evidence_workspace
        has_evidence = (
            bool(workspace) and len(workspace) > 0
            if request.answer_options.evidence_source == "workspace"
            else bool(package.context_bundle.items)
        )
        if request.policy is None and (package.retrieval_state == "EMPTY" or not has_evidence):
            unresolved_ids = set(package.unresolved_requirements)
            unresolved = "；".join(
                item.target
                for item in package.evidence_plan.requirements
                if item.id in unresolved_ids
            )
            suffix = f" 当前仍缺少：{unresolved}。" if unresolved else ""
            return _AnswerOutcome(AnswerResult(EMPTY_CONTEXT_ANSWER + suffix, []))
        mode = request.answer_options.answer_mode
        if request.policy is not None and mode == "teach":
            return self._answer_teach(request, package)
        generator = self.answer_generator_factory()
        if mode == "legacy":
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
            return _AnswerOutcome(
                answer_result=result,
                stages=[
                    StageUsage(
                        "answer_generation",
                        (time.perf_counter() - started) * 1000,
                        "legacy",
                    )
                ],
                evidence_catalog=package.evidence_catalog,
            )
        if mode == "explain":
            return self._answer_explain(request, package, generator)
        if mode == "teach":
            return self._answer_teach(request, package)
        # An unrecognised mode must not fall through to explain: that would make
        # the deep-depth contract unenforceable one layer up.
        raise ValueError(f"unsupported answer_mode: {mode!r}")

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
        should_review = len(answer_plan.sections) >= 3 or bool(answer_plan.conflicts) or not draft.used_citations or bool(draft.invalid_citations) or answer_plan.decision_source == "fallback" or _draft_structure_failed(draft)
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
        return _AnswerOutcome(
            answer_result=generator.finalize_draft(draft),
            answer_plan=answer_plan,
            review=review,
            stages=stages,
            evidence_catalog=package.evidence_catalog,
        )

    def _answer_teach(
        self,
        request: UserRequest,
        package: EvidencePackage,
    ) -> _AnswerOutcome:
        """Plan how to explain, then write from that plan.

        Reviewing is not part of this stage: the teaching reviewer lands with the
        rest of the answer-quality work, and a legacy reviewer expecting an
        AnswerPlan would be measuring the wrong thing.
        """
        if self.teaching_workflow is None:
            raise RuntimeError("teaching workflow is unavailable")
        if getattr(self, "on_section", None) is not None:
            result = self.teaching_workflow.run(request, package, on_section=self.on_section)
        else:
            result = self.teaching_workflow.run(request, package)
        return _AnswerOutcome(
            answer_result=result.answer,
            explanation_plan=result.explanation_plan,
            stages=list(result.stages),
            context_bundle=result.context_bundle,
            evidence_catalog=package.evidence_catalog,
            workspace_stats=result.stats,
        )


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
    goal = getattr(answer_plan, "answer_goal", None) or request.original_query
    strategy = getattr(answer_plan, "explanation_strategy", None) or "mixed"
    return {
        "original_query": request.original_query,
        "intent_summary": request.original_query,
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


def _draft_structure_failed(draft: GroundedDraft) -> bool:
    headings = re.findall(r"(?m)^#{1,6}\s+(.+)$", draft.text_with_citations)
    normalized = [" ".join(h.split()).casefold() for h in headings]
    return not draft.text_with_citations.strip() or len(normalized) != len(set(normalized))


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
