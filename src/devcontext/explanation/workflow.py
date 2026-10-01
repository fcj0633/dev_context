from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from devcontext.context import ContextBuilder, EvidenceRef, EvidenceWorkspace
from devcontext.context.budget import (
    FALLBACK_CAPABILITIES,
    ModelCapabilities,
    TokenBudgetPolicy,
)
from devcontext.context.estimator import HeuristicTokenEstimator, TokenEstimator
from devcontext.context.views import build_context_view
from devcontext.explanation.budget import OutputBudget, budget_for
from devcontext.explanation.composer import SectionComposer
from devcontext.explanation.grounding import GroundingIssue, grounding_issues
from devcontext.explanation.models import (
    DraftSection,
    ExplanationPlan,
    ExplanationSection,
)
from devcontext.explanation.planner import (
    ExplanationPlanner,
    fallback_explanation_plan,
)
from devcontext.explanation.reviewer import (
    MAX_REVISION_ROUNDS,
    TeachingReviewResult,
    TeachingReviewer,
    needs_llm_review,
)
from devcontext.explanation.writer import TeachingDraft, TeachingWriter
from devcontext.models import AnswerResult, ContextBundle
from devcontext.observability import (
    SectionExecutionTrace,
    mark_last_call_wasted,
    record_section_execution,
)
from devcontext.request import UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage
    from devcontext.agentic.models import StageUsage


# Fallback only. The real ceiling comes from the model's window via
# TokenBudgetPolicy; this is what a caller with no model configured gets.
DEFAULT_TEACHING_MAX_CHARS = 200_000
# Prompt scaffolding that is not evidence: instructions, the plan, the question.
FIXED_PROMPT_TOKENS = 4_000


@dataclass(frozen=True, slots=True)
class TeachingAnswerResult:
    answer: AnswerResult
    explanation_plan: ExplanationPlan | None
    # What the answer was written from, under workspace labels.
    context_bundle: ContextBundle
    draft: TeachingDraft | None
    stages: tuple[StageUsage, ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)
    section_drafts: tuple[DraftSection, ...] = ()
    grounding_issues: tuple[GroundingIssue, ...] = ()
    budget: OutputBudget | None = None
    review: TeachingReviewResult | None = None
    # Per-section costs. Child diagnostics of the teaching_draft stage, not a
    # partition of the request: they are counted inside it, never beside it.
    section_traces: tuple[SectionExecutionTrace, ...] = ()


class TeachingExplanationWorkflow:
    """Retrieval has already answered "what is true". This answers "how to teach it".

    Two paths. A short answer is written in one pass. A long one is written
    section by section against section-scoped evidence and then composed, because
    a single pass over eight sections stops holding the argument together - and
    because per-section evidence is what makes a citation enforceable.
    """

    def __init__(
        self,
        planner: ExplanationPlanner,
        writer: TeachingWriter,
        composer: SectionComposer | None = None,
        reviewer: TeachingReviewer | None = None,
        *,
        max_chars: int = DEFAULT_TEACHING_MAX_CHARS,
        capabilities: ModelCapabilities | None = None,
        estimator: TokenEstimator | None = None,
    ) -> None:
        self.planner = planner
        self.writer = writer
        self.composer = composer
        self.reviewer = reviewer
        self.capabilities = capabilities or FALLBACK_CAPABILITIES
        self.estimator = estimator or HeuristicTokenEstimator()
        # The stage sizes each view from the model's window rather than from a
        # character ceiling; model_capabilities alone made that possible.
        self.policy = TokenBudgetPolicy(self.capabilities)
        self.max_chars = max_chars

    def run(
        self,
        request: UserRequest,
        evidence_package: EvidencePackage,
    ) -> TeachingAnswerResult:
        stages: list[Any] = []

        started = time.perf_counter()
        planning_error: str | None = None
        try:
            plan = self.planner.plan(request, evidence_package)
        except Exception as exception:
            # Recording only "fallback" hid why: the planner runs for ~14s and
            # emits thousands of tokens before validation rejects it, so the
            # reason is the only thing that makes the fallback diagnosable.
            from devcontext.agentic.models import error_detail

            planning_error = error_detail(exception)
            mark_last_call_wasted(
                "explanation plan rejected; fell back", stage="explanation_planning"
            )
            plan = fallback_explanation_plan(request, evidence_package)
        stages.append(
            _stage("explanation_planning", started, plan.decision_source,
                   getattr(self.planner, "last_client", None), "high")
        )

        budget = budget_for(plan, self.capabilities)
        self._view_max_chars = self._view_char_budget(
            request.original_query, plan, budget
        )
        bound = self._bound_bundle(request.original_query, evidence_package, plan)

        draft: TeachingDraft | None
        drafts: tuple[DraftSection, ...] = ()
        issues: tuple[GroundingIssue, ...] = ()
        review: TeachingReviewResult | None = None
        error: str | None = None
        section_traces: list[SectionExecutionTrace] = []

        started = time.perf_counter()
        view_sizes: dict[str, int] = {}
        if budget.allow_multi_pass and self.composer is not None and bound.items:
            draft, drafts, issues, error = self._write_in_sections(
                request.original_query, evidence_package, plan, view_sizes,
                section_traces,
            )
        elif bound.items:
            try:
                draft = self.writer.write(request.original_query, bound, plan)
            except Exception as exception:
                from devcontext.agentic.models import error_detail

                error = error_detail(exception)
                draft = None
        else:
            draft = None
        stages.append(
            _stage(
                "teaching_draft", started, "fallback" if error else "llm",
                getattr(self.writer, "last_client", None), "high",
            )
        )

        # Review runs on the section-level path, where naming a bad section is
        # actionable. A one-shot answer has no sections to target.
        if drafts and self.reviewer is not None and needs_llm_review(plan, issues):
            started_review = time.perf_counter()
            review = self.reviewer.review(request.original_query, plan, drafts)
            stages.append(
                _stage("teaching_review", started_review, review.decision_source,
                       getattr(self.reviewer, "last_client", None), "high")
            )
            if review.revision_required and MAX_REVISION_ROUNDS >= 1:
                started_revision = time.perf_counter()
                drafts, revised = self._revise(
                    request.original_query, evidence_package, plan, drafts, review,
                    section_traces,
                )
                issues = issues + revised
                draft = self.composer.compose(request.original_query, plan, drafts)
                stages.append(
                    _stage("teaching_revision", started_revision, "llm",
                           getattr(self.writer, "last_client", None), "high")
                )

        answer = AnswerResult(
            draft.text_with_citations if draft else _no_evidence_answer(),
            list(draft.used_citations) if draft else [],
            list(draft.invalid_citations) if draft else [],
            zero_valid_citation=not (draft and draft.used_citations),
        )
        invalid = sum(len(item.invalid_citations) for item in drafts) + len(
            answer.invalid_citations
        )
        return TeachingAnswerResult(
            answer=answer,
            explanation_plan=plan,
            context_bundle=bound,
            draft=draft,
            stages=tuple(stages),
            stats={
                "path": "multi_pass" if drafts else "fast",
                "bound_evidence_count": len(bound.items),
                "plan_section_count": len(plan.sections),
                "plan_evidence_labels": list(plan.evidence_labels),
                "planning_error": planning_error,
                "grounding_issues": [item.to_dict() for item in issues],
                "review": review.to_dict() if review else None,
                "invalid_citation_count": invalid,
                "output_budget": budget.to_dict(),
                "view_max_chars": self._view_max_chars,
                "draft_error": error,
                "trace": _trace_block(
                    plan, drafts, review, bound, evidence_package, view_sizes, budget,
                    planning_error, section_traces,
                ),
            },
            section_drafts=drafts,
            grounding_issues=issues,
            budget=budget,
            review=review,
            section_traces=tuple(section_traces),
        )

    def _view(
        self,
        query: str,
        evidence_package: EvidencePackage,
        labels: tuple[str, ...],
    ) -> ContextBundle:
        workspace = evidence_package.evidence_workspace
        if workspace is None:
            # No workspace means this predates it; fall back to whatever the
            # labels resolve to in the retrieval context.
            wanted = set(labels)
            refs = tuple(
                _ref_from_item(item)
                for item in evidence_package.context_bundle.items
                if item.citation.label in wanted
            )
            return _bundle(query, refs, self._current_view_budget())
        view = build_context_view(
            workspace,
            evidence_labels=labels,
            max_chars=self._current_view_budget(),
            query=query,
        )
        return view.to_bundle()

    def _current_view_budget(self) -> int:
        return getattr(self, "_view_max_chars", self.max_chars)

    def _revise(
        self,
        query: str,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
        drafts: tuple[DraftSection, ...],
        review: TeachingReviewResult,
        section_traces: list[SectionExecutionTrace],
    ) -> tuple[tuple[DraftSection, ...], tuple[GroundingIssue, ...]]:
        """Regenerate only the sections the reviewer named."""
        notes: dict[str, list[str]] = {}
        for issue in review.section_issues:
            notes.setdefault(issue.section_id, []).append(
                f"{issue.issue_type}: {issue.description}"
            )
        by_id = {section.id: section for section in plan.sections}
        revised: list[DraftSection] = []
        new_issues: list[GroundingIssue] = []
        for draft in drafts:
            if draft.section_id not in review.revision_required:
                revised.append(draft)
                continue
            section = by_id.get(draft.section_id)
            context = (
                self._section_bundle(query, evidence_package, section)
                if section is not None
                else None
            )
            if section is None or context is None or not context.items:
                revised.append(draft)
                continue
            revision_notes = tuple(notes.get(draft.section_id, []))
            started = time.perf_counter()
            try:
                replacement = self.writer.write_section(
                    query, plan.core_mental_model, section, context,
                    revision_notes=revision_notes,
                )
            except Exception as exception:
                from devcontext.agentic.models import error_detail

                section_traces.append(record_section(
                    section, context, started, self.writer.last_client,
                    revision=True, revision_notes_count=len(revision_notes),
                    success=False, error=error_detail(exception),
                ))
                revised.append(draft)
                continue
            section_traces.append(record_section(
                section, context, started, self.writer.last_client,
                revision=True, revision_notes_count=len(revision_notes),
            ))
            revised.append(replacement)
            new_issues.extend(
                grounding_issues(section, replacement.text_with_citations)
            )
        return tuple(revised), tuple(new_issues)

    def _write_in_sections(
        self,
        query: str,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
        view_sizes: dict[str, int],
        section_traces: list[SectionExecutionTrace],
    ) -> tuple[TeachingDraft | None, tuple[DraftSection, ...], tuple[GroundingIssue, ...], str | None]:
        drafts: list[DraftSection] = []
        issues: list[GroundingIssue] = []
        for section in plan.sections:
            context = self._section_bundle(query, evidence_package, section)
            view_sizes[section.id] = len(context.items)
            if not context.items:
                # Nothing bound to this section, so there is nothing to write from.
                issues.append(GroundingIssue(
                    section.id, "SECTION_WITHOUT_EVIDENCE",
                    "该章节没有绑定任何证据，无法生成",
                ))
                section_traces.append(record_section(
                    section, context, None, success=False,
                    error="SECTION_WITHOUT_EVIDENCE",
                ))
                continue
            started = time.perf_counter()
            try:
                draft = self.writer.write_section(
                    query, plan.core_mental_model, section, context
                )
            except Exception as exception:
                from devcontext.agentic.models import error_detail

                detail = error_detail(exception)
                section_traces.append(record_section(
                    section, context, started, self.writer.last_client,
                    success=False, error=detail,
                ))
                return None, tuple(drafts), tuple(issues), detail
            section_traces.append(record_section(
                section, context, started, self.writer.last_client,
            ))
            drafts.append(draft)
            issues.extend(grounding_issues(section, draft.text_with_citations))
        if not drafts:
            return None, (), tuple(issues), None
        composed = self.composer.compose(query, plan, drafts)
        return composed, tuple(drafts), tuple(issues), None

    def _view_char_budget(
        self,
        query: str,
        plan: ExplanationPlan,
        budget: OutputBudget,
    ) -> int:
        """Turn the model's window into a size for one view, in tokens."""
        fixed = (
            FIXED_PROMPT_TOKENS
            + self.estimator.estimate(query)
            + self.estimator.estimate(json.dumps(plan.to_dict(), ensure_ascii=False))
        )
        decision = self.policy.decide(
            fixed_tokens=fixed, requested_output_tokens=budget.max_output_tokens
        )
        if not decision.allowed or decision.context_budget_tokens < 1:
            return 0
        return self.policy.context_chars_for(decision, self.estimator)

    def _bound_bundle(
        self,
        query: str,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
    ) -> ContextBundle:
        return self._view(query, evidence_package, plan.evidence_labels)

    def _section_bundle(
        self,
        query: str,
        evidence_package: EvidencePackage,
        section: ExplanationSection,
    ) -> ContextBundle:
        return self._view(query, evidence_package, section.evidence_labels)


def _trace_block(
    plan: ExplanationPlan,
    drafts: tuple[DraftSection, ...],
    review: TeachingReviewResult | None,
    bound: ContextBundle,
    evidence_package: EvidencePackage,
    view_sizes: dict[str, int],
    budget: OutputBudget,
    planning_error: str | None = None,
    section_traces: Sequence[SectionExecutionTrace] = (),
) -> dict[str, Any]:
    """The teach path's intermediate state, shaped for --debug and for interviews."""
    workspace = evidence_package.evidence_workspace
    return {
        "decision_source": plan.decision_source,
        "planning_error": planning_error,
        "primary_strategy": plan.primary_strategy,
        "secondary_strategies": list(plan.secondary_strategies),
        "core_mental_model": plan.core_mental_model,
        "context_views": {
            "workspace_evidence": len(workspace) if workspace is not None else None,
            "bound_evidence": len(bound.items),
            "per_section": dict(view_sizes),
        },
        "section_drafts": [
            {
                "id": draft.section_id,
                "title": draft.title,
                "evidence_state": draft.evidence_state,
                "used_citations": list(draft.used_citations),
                "invalid_citations": list(draft.invalid_citations),
                "chars": len(draft.text_with_citations),
            }
            for draft in drafts
        ],
        "section_citations": {
            draft.section_id: list(draft.used_citations) for draft in drafts
        },
        "section_execution": [item.to_dict() for item in section_traces],
        "section_confidence": {
            draft.section_id: draft.evidence_state for draft in drafts
        },
        # No compression happens yet: nothing in this round drops evidence from a
        # view, so the list is empty rather than absent - a reader should be able
        # to tell "none happened" from "not recorded".
        "compression_events": [],
        "revision_trace": review.to_dict() if review is not None else None,
        "output_budget": budget.to_dict(),
    }


def _bound_refs(
    evidence_package: EvidencePackage,
    labels: tuple[str, ...],
) -> tuple[EvidenceRef, ...]:
    workspace = evidence_package.evidence_workspace
    if workspace is not None:
        return workspace.by_citation(labels)
    # No workspace means this predates the workspace change; fall back to
    # whatever the labels resolve to in the retrieval context.
    wanted = set(labels)
    return tuple(
        _ref_from_item(item)
        for item in evidence_package.context_bundle.items
        if item.citation.label in wanted
    )


def _bundle(query: str, refs: tuple[EvidenceRef, ...], max_chars: int) -> ContextBundle:
    if max_chars < 1:
        # A window too small to hold anything is a legal outcome of the budget
        # policy, not an error: it means this model cannot carry this prompt.
        return ContextBundle(query=query, items=[], rendered_text="", total_chars=0,
                             max_chars=0, truncated=bool(refs))
    items = [context_item_from_ref(ref) for ref in refs]
    rendered, kept, truncated = ContextBuilder(max_chars=max_chars).render_items(items)
    return ContextBundle(
        query=query,
        items=kept,
        rendered_text=rendered,
        total_chars=len(rendered),
        max_chars=max_chars,
        truncated=truncated,
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


def _no_evidence_answer() -> str:
    from devcontext.answer.generator import EMPTY_CONTEXT_ANSWER

    return EMPTY_CONTEXT_ANSWER


def record_section(
    section: ExplanationSection,
    context: ContextBundle,
    started: float | None,
    client: object | None = None,
    *,
    revision: bool = False,
    revision_notes_count: int = 0,
    success: bool = True,
    error: str | None = None,
) -> SectionExecutionTrace:
    """Build, record and return the cost of generating one section."""
    usage = getattr(client, "last_usage", {}) if client is not None else {}
    if not isinstance(usage, dict):
        usage = {}
    trace = SectionExecutionTrace(
        section_id=section.id,
        title=section.title,
        section_type=section.section_type,
        latency_ms=0.0 if started is None else (time.perf_counter() - started) * 1000,
        evidence_count=len(context.items),
        context_chars=context.total_chars,
        target_tokens=section.target_tokens,
        revision=revision,
        revision_notes_count=revision_notes_count,
        input_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
        output_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
        success=success,
        error=error,
    )
    record_section_execution(trace)
    return trace


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
