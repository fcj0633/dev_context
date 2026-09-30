from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from devcontext.context import ContextBuilder, EvidenceRef, EvidenceWorkspace
from devcontext.context.views import context_item_from_ref
from devcontext.explanation.models import ExplanationPlan
from devcontext.explanation.planner import (
    ExplanationPlanner,
    fallback_explanation_plan,
)
from devcontext.explanation.writer import TeachingDraft, TeachingWriter
from devcontext.models import AnswerResult, ContextBundle
from devcontext.request import UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage
    from devcontext.agentic.models import StageUsage


DEFAULT_TEACHING_MAX_CHARS = 200_000


@dataclass(frozen=True, slots=True)
class TeachingAnswerResult:
    answer: AnswerResult
    explanation_plan: ExplanationPlan | None
    # Only the evidence the plan bound, under its workspace labels, and only the
    # part of it that actually fits the writer's prompt.
    context_bundle: ContextBundle
    draft: TeachingDraft | None
    stages: tuple[StageUsage, ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)


class TeachingExplanationWorkflow:
    """Retrieval has already answered "what is true". This answers "how to teach it".

    The evidence package carries the whole workspace, so this stage decides for
    itself how much evidence to put in front of the writer instead of inheriting
    a bundle that was already cut to fit one prompt.
    """

    def __init__(
        self,
        planner: ExplanationPlanner,
        writer: TeachingWriter,
        *,
        max_chars: int = DEFAULT_TEACHING_MAX_CHARS,
    ) -> None:
        self.planner = planner
        self.writer = writer
        self.max_chars = max_chars

    def run(
        self,
        request: UserRequest,
        evidence_package: EvidencePackage,
    ) -> TeachingAnswerResult:
        from devcontext.agentic.models import StageUsage, error_detail

        stages: list[StageUsage] = []

        started = time.perf_counter()
        try:
            plan = self.planner.plan(request, evidence_package)
        except Exception:
            plan = fallback_explanation_plan(request, evidence_package)
        stages.append(
            _stage("explanation_planning", started, plan.decision_source,
                   getattr(self.planner, "last_client", None), "high")
        )

        bundle = self._bound_bundle(request.original_query, evidence_package, plan)

        started = time.perf_counter()
        draft: TeachingDraft | None = None
        error: str | None = None
        if bundle.items:
            try:
                draft = self.writer.write(request.original_query, bundle, plan)
            except Exception as exception:
                error = error_detail(exception)
        stages.append(
            _stage("teaching_draft", started, "fallback" if error else "llm",
                   getattr(self.writer, "last_client", None), "high")
        )

        answer = AnswerResult(
            draft.text_with_citations if draft else _no_evidence_answer(bundle),
            list(draft.used_citations) if draft else [],
            list(draft.invalid_citations) if draft else [],
            zero_valid_citation=not (draft and draft.used_citations),
        )
        return TeachingAnswerResult(
            answer=answer,
            explanation_plan=plan,
            context_bundle=bundle,
            draft=draft,
            stages=tuple(stages),
            stats={
                "bound_evidence_count": len(bundle.items),
                "plan_section_count": len(plan.sections),
                "plan_evidence_labels": list(plan.evidence_labels),
                "draft_error": error,
            },
        )

    def _bound_bundle(
        self,
        query: str,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
    ) -> ContextBundle:
        workspace = evidence_package.evidence_workspace
        refs = self._bound_refs(workspace, evidence_package, plan)
        items = [context_item_from_ref(ref) for ref in refs]
        rendered, kept, truncated = ContextBuilder(
            max_chars=self.max_chars
        ).render_items(items)
        return ContextBundle(
            query=query,
            items=kept,
            rendered_text=rendered,
            total_chars=len(rendered),
            max_chars=self.max_chars,
            truncated=truncated,
        )

    @staticmethod
    def _bound_refs(
        workspace: EvidenceWorkspace | None,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
    ) -> tuple[EvidenceRef, ...]:
        if workspace is not None:
            return workspace.by_citation(plan.evidence_labels)
        # No workspace means this predates the workspace change; fall back to
        # whatever the plan's labels resolve to in the retrieval context.
        wanted = set(plan.evidence_labels)
        return tuple(
            _ref_from_item(item)
            for item in evidence_package.context_bundle.items
            if item.citation.label in wanted
        )


def _ref_from_item(item: Any) -> EvidenceRef:
    return EvidenceRef(
        chunk_id=item.chunk_id,
        evidence_id=item.citation.label,
        citation=item.citation,
        requirement_ids=tuple(item.sub_question_ids),
        source_role=item.source_role,
        temporal_status=item.temporal_status,
        authority_priority=item.authority_priority,
        chunk_type=item.chunk_type,
        first_seen_round=0,
        retrieval_rank=item.retrieval_rank,
        retrieval_score=item.score,
        content=item.content,
    )


def _no_evidence_answer(bundle: ContextBundle) -> str:
    from devcontext.answer.generator import EMPTY_CONTEXT_ANSWER

    return EMPTY_CONTEXT_ANSWER


def _stage(stage, started, decision_source, client, effort):
    from devcontext.agentic.models import StageUsage

    usage = getattr(client, "last_usage", {}) if client is not None else {}
    if not isinstance(usage, dict):
        usage = {}
    return StageUsage(
        stage,
        (time.perf_counter() - started) * 1000,
        decision_source,
        getattr(client, "model", None),
        getattr(client, "reasoning_effort", effort),
        usage.get("prompt_tokens", usage.get("input_tokens")),
        usage.get("completion_tokens", usage.get("output_tokens")),
    )
