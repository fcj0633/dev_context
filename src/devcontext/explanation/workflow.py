from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from devcontext.context import ContextBuilder, EvidenceRef, EvidenceWorkspace
from devcontext.context.budget import FALLBACK_CAPABILITIES, ModelCapabilities
from devcontext.context.views import context_item_from_ref
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
from devcontext.request import UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage
    from devcontext.agentic.models import StageUsage


DEFAULT_TEACHING_MAX_CHARS = 200_000


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
    ) -> None:
        self.planner = planner
        self.writer = writer
        self.composer = composer
        self.reviewer = reviewer
        self.max_chars = max_chars
        self.capabilities = capabilities or FALLBACK_CAPABILITIES

    def run(
        self,
        request: UserRequest,
        evidence_package: EvidencePackage,
    ) -> TeachingAnswerResult:
        stages: list[Any] = []

        started = time.perf_counter()
        try:
            plan = self.planner.plan(request, evidence_package)
        except Exception:
            plan = fallback_explanation_plan(request, evidence_package)
        stages.append(
            _stage("explanation_planning", started, plan.decision_source,
                   getattr(self.planner, "last_client", None), "high")
        )

        budget = budget_for(plan, self.capabilities)
        bound = self._bound_bundle(request.original_query, evidence_package, plan)

        draft: TeachingDraft | None
        drafts: tuple[DraftSection, ...] = ()
        issues: tuple[GroundingIssue, ...] = ()
        review: TeachingReviewResult | None = None
        error: str | None = None

        started = time.perf_counter()
        if budget.allow_multi_pass and self.composer is not None and bound.items:
            draft, drafts, issues, error = self._write_in_sections(
                request.original_query, evidence_package, plan
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
                    request.original_query, evidence_package, plan, drafts, review
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
                "grounding_issues": [item.to_dict() for item in issues],
                "review": review.to_dict() if review else None,
                "invalid_citation_count": invalid,
                "output_budget": budget.to_dict(),
                "draft_error": error,
            },
            section_drafts=drafts,
            grounding_issues=issues,
            budget=budget,
            review=review,
        )

    def _revise(
        self,
        query: str,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
        drafts: tuple[DraftSection, ...],
        review: TeachingReviewResult,
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
            try:
                replacement = self.writer.write_section(
                    query, plan.core_mental_model, section, context,
                    revision_notes=tuple(notes.get(draft.section_id, [])),
                )
            except Exception:
                revised.append(draft)
                continue
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
    ) -> tuple[TeachingDraft | None, tuple[DraftSection, ...], tuple[GroundingIssue, ...], str | None]:
        drafts: list[DraftSection] = []
        issues: list[GroundingIssue] = []
        for section in plan.sections:
            context = self._section_bundle(query, evidence_package, section)
            if not context.items:
                # Nothing bound to this section, so there is nothing to write from.
                issues.append(GroundingIssue(
                    section.id, "SECTION_WITHOUT_EVIDENCE",
                    "该章节没有绑定任何证据，无法生成",
                ))
                continue
            try:
                draft = self.writer.write_section(
                    query, plan.core_mental_model, section, context
                )
            except Exception as exception:
                from devcontext.agentic.models import error_detail

                return None, tuple(drafts), tuple(issues), error_detail(exception)
            drafts.append(draft)
            issues.extend(grounding_issues(section, draft.text_with_citations))
        if not drafts:
            return None, (), tuple(issues), None
        composed = self.composer.compose(query, plan, drafts)
        return composed, tuple(drafts), tuple(issues), None

    def _bound_bundle(
        self,
        query: str,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
    ) -> ContextBundle:
        refs = _bound_refs(evidence_package, plan.evidence_labels)
        return _bundle(query, refs, self.max_chars)

    def _section_bundle(
        self,
        query: str,
        evidence_package: EvidencePackage,
        section: ExplanationSection,
    ) -> ContextBundle:
        refs = _bound_refs(evidence_package, section.evidence_labels)
        return _bundle(query, refs, self.max_chars)


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
