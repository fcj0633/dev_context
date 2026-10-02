from __future__ import annotations

import json
import contextvars
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from devcontext.explanation.budget import (
    OutputBudget,
    WRITER_REASONING_RESERVE_TOKENS,
    budget_for,
    section_max_tokens,
    single_pass_max_tokens,
)
from devcontext.explanation.composer import SectionComposer, deterministic_join
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
from devcontext.explanation.policy import TeachingRuntimeOptions
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
    current,
    last_completed_call_id,
    llm_attempt,
    mark_call_wasted,
    parallel_group,
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


@dataclass(frozen=True, slots=True)
class _SectionTaskResult:
    section_index: int
    section_id: str
    draft: DraftSection | None
    grounding_issues: tuple[GroundingIssue, ...]
    section_trace: SectionExecutionTrace
    llm_call_ids: tuple[int, ...]
    attempts: int
    error: str | None = None


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
        runtime_options: TeachingRuntimeOptions | None = None,
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
        # Direct construction remains conservative for old callers. The CLI
        # injects the validated Settings-derived options explicitly.
        self.runtime_options = runtime_options or TeachingRuntimeOptions(
            depth_policy="legacy", section_concurrency=1
        )

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
            mark_call_wasted(
                last_completed_call_id(),
                f"explanation plan rejected; fallback: {planning_error}",
                stage="explanation_planning",
            )
            plan = fallback_explanation_plan(
                request,
                evidence_package,
                getattr(self.planner, "last_depth_decision", None),
                getattr(self.planner, "last_section_budget", None),
            )
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
        failed_section_ids: list[str] = []
        partial = False

        started = time.perf_counter()
        view_sizes: dict[str, int] = {}
        if budget.allow_multi_pass and self.composer is not None and bound.items:
            draft, drafts, issues, error, failed = self._write_in_sections(
                request.original_query, evidence_package, plan, view_sizes,
                section_traces,
            )
            failed_section_ids.extend(failed)
            partial = bool(drafts and failed)
        elif bound.items:
            try:
                draft = self.writer.write(
                    request.original_query,
                    bound,
                    plan,
                    max_output_tokens=single_pass_max_tokens(
                        plan, self.capabilities
                    ),
                )
            except Exception as exception:
                from devcontext.agentic.models import error_detail

                error = error_detail(exception)
                draft = None
        else:
            draft = None
        stages.append(
            _stage(
                "teaching_draft",
                started,
                (
                    "skipped_no_evidence"
                    if not bound.items
                    else "partial"
                    if partial
                    else "fallback"
                    if error
                    else "llm"
                ),
                (
                    getattr(self.writer, "last_client", None)
                    if not (budget.allow_multi_pass and self.runtime_options.section_concurrency > 1)
                    else None
                ),
                "high",
            )
        )

        # Review runs on the section-level path, where naming a bad section is
        # actionable. A one-shot answer has no sections to target.
        if (
            drafts
            and not partial
            and self.reviewer is not None
            and needs_llm_review(plan, issues)
        ):
            started_review = time.perf_counter()
            review = self.reviewer.review(request.original_query, plan, drafts)
            stages.append(
                _stage("teaching_review", started_review, review.decision_source,
                       getattr(self.reviewer, "last_client", None), "high")
            )
            if review.revision_required and MAX_REVISION_ROUNDS >= 1:
                started_revision = time.perf_counter()
                drafts, revised, revision_failed = self._revise(
                    request.original_query, evidence_package, plan, drafts, review,
                    section_traces,
                )
                failed_section_ids.extend(revision_failed)
                issues = issues + revised
                draft = self.composer.compose(request.original_query, plan, drafts)
                stages.append(
                    _stage("teaching_revision", started_revision, "llm",
                           (
                               getattr(self.writer, "last_client", None)
                               if self.runtime_options.section_concurrency == 1
                               else None
                           ), "high")
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
                "partial_answer": partial,
                "failed_section_ids": list(dict.fromkeys(failed_section_ids)),
                "trace": _trace_block(
                    plan, drafts, review, bound, evidence_package, view_sizes, budget,
                    planning_error, section_traces,
                    planner=self.planner,
                    failed_section_ids=tuple(dict.fromkeys(failed_section_ids)),
                    partial=partial,
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
    ) -> tuple[
        tuple[DraftSection, ...], tuple[GroundingIssue, ...], tuple[str, ...]
    ]:
        """Regenerate only the sections the reviewer named."""
        notes: dict[str, list[str]] = {}
        for issue in review.section_issues:
            notes.setdefault(issue.section_id, []).append(
                f"{issue.issue_type}: {issue.description}"
            )
        by_id = {section.id: section for section in plan.sections}
        replacements: dict[str, DraftSection] = {}
        new_issues: list[GroundingIssue] = []
        failed: list[str] = []
        tasks: list[tuple[int, ExplanationSection, ContextBundle, tuple[str, ...]]] = []
        draft_by_id = {draft.section_id: draft for draft in drafts}
        for index, draft in enumerate(drafts):
            if draft.section_id not in review.revision_required:
                continue
            section = by_id.get(draft.section_id)
            context = (
                self._section_bundle(query, evidence_package, section)
                if section is not None
                else None
            )
            if section is None or context is None or not context.items:
                failed.append(draft.section_id)
                continue
            revision_notes = tuple(notes.get(draft.section_id, []))
            tasks.append((index, section, context, revision_notes))

        results = self._run_section_tasks(
            query,
            plan,
            tasks,
            group_id="teaching_revisions",
            revision=True,
        )
        for result in results:
            section_traces.append(result.section_trace)
            if result.draft is None:
                failed.append(result.section_id)
                continue
            replacements[result.section_id] = result.draft
            new_issues.extend(result.grounding_issues)

        revised = tuple(
            replacements.get(draft.section_id, draft) for draft in drafts
        )
        # A failed revision intentionally preserves the original draft.
        assert all(draft.section_id in draft_by_id for draft in revised)
        return revised, tuple(new_issues), tuple(dict.fromkeys(failed))

    def _run_section_tasks(
        self,
        query: str,
        plan: ExplanationPlan,
        tasks: Sequence[
            tuple[int, ExplanationSection, ContextBundle, tuple[str, ...]]
        ],
        *,
        group_id: str,
        revision: bool,
    ) -> list[_SectionTaskResult]:
        if not tasks:
            return []
        concurrency = min(self.runtime_options.section_concurrency, len(tasks))
        if concurrency == 1:
            return [
                self._generate_section_task(
                    query, plan, index, section, context, notes,
                    group_id=group_id,
                    revision=revision,
                    remember_last_client=True,
                )
                for index, section, context, notes in tasks
            ]

        results: list[_SectionTaskResult] = []
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="teaching-section",
        ) as executor:
            futures = []
            for index, section, context, notes in tasks:
                # A Context cannot be entered by two threads at once. Copy once
                # per submitted task, never once per executor/fan-out.
                ctx = contextvars.copy_context()
                futures.append(executor.submit(
                    ctx.run,
                    self._generate_section_task,
                    query,
                    plan,
                    index,
                    section,
                    context,
                    notes,
                    group_id=group_id,
                    revision=revision,
                    remember_last_client=False,
                ))
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item.section_index)
        return results

    def _generate_section_task(
        self,
        query: str,
        plan: ExplanationPlan,
        section_index: int,
        section: ExplanationSection,
        context: ContextBundle,
        revision_notes: tuple[str, ...],
        *,
        group_id: str,
        revision: bool,
        remember_last_client: bool,
    ) -> _SectionTaskResult:
        from devcontext.agentic.models import error_detail

        started = time.perf_counter()
        recorder = current()
        started_offset = recorder.offset_ms() if recorder is not None else 0.0
        call_ids: list[int] = []
        last_error: str | None = None
        attempts = 0
        draft: DraftSection | None = None
        for attempt in (1, 2):
            attempts = attempt
            prior_call = last_completed_call_id()
            try:
                with parallel_group(group_id), llm_attempt(attempt):
                    draft = self.writer.write_section(
                        query,
                        plan.core_mental_model,
                        section,
                        context,
                        revision_notes=revision_notes,
                        trace_stage=(
                            "teaching_revision" if revision else "teaching_draft"
                        ),
                        max_output_tokens=section_max_tokens(
                            plan, section, self.capabilities
                        ),
                        remember_last_client=remember_last_client,
                    )
                current_call = last_completed_call_id()
                if current_call is not None and current_call != prior_call:
                    call_ids.append(current_call)
                if draft.invalid_citations:
                    last_error = "CITATION_VALIDATION_ERROR"
                    if call_ids:
                        mark_call_wasted(
                            call_ids[-1],
                            "section output discarded; invalid citation",
                            stage=(
                                "teaching_revision"
                                if revision
                                else "teaching_draft"
                            ),
                        )
                    draft = None
                break
            except Exception as exception:
                current_call = last_completed_call_id()
                if current_call is not None and current_call != prior_call:
                    call_ids.append(current_call)
                last_error = error_detail(exception)
                if attempt == 1 and _is_transient_failure(exception, last_error):
                    continue
                break

        task_issues = (
            tuple(grounding_issues(section, draft.text_with_citations))
            if draft is not None
            else ()
        )
        trace = record_section(
            section,
            context,
            started,
            revision=revision,
            revision_notes_count=len(revision_notes),
            success=draft is not None,
            error=None if draft is not None else last_error,
            call_ids=tuple(call_ids),
            attempt_count=attempts,
            started_offset_ms=started_offset,
        )
        return _SectionTaskResult(
            section_index,
            section.id,
            draft,
            task_issues,
            trace,
            tuple(call_ids),
            attempts,
            None if draft is not None else last_error,
        )

    def _write_in_sections(
        self,
        query: str,
        evidence_package: EvidencePackage,
        plan: ExplanationPlan,
        view_sizes: dict[str, int],
        section_traces: list[SectionExecutionTrace],
    ) -> tuple[
        TeachingDraft | None,
        tuple[DraftSection, ...],
        tuple[GroundingIssue, ...],
        str | None,
        tuple[str, ...],
    ]:
        issues: list[GroundingIssue] = []
        failed: list[str] = []
        tasks: list[tuple[int, ExplanationSection, ContextBundle, tuple[str, ...]]] = []
        for index, section in enumerate(plan.sections):
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
                    error="SECTION_WITHOUT_EVIDENCE", attempt_count=0,
                ))
                failed.append(section.id)
                continue
            tasks.append((index, section, context, ()))

        results = self._run_section_tasks(
            query, plan, tasks, group_id="teaching_sections", revision=False
        )
        drafts: list[DraftSection] = []
        errors: list[str] = []
        for result in results:
            section_traces.append(result.section_trace)
            if result.draft is None:
                failed.append(result.section_id)
                if result.error:
                    errors.append(f"{result.section_id}: {result.error}")
                continue
            drafts.append(result.draft)
            issues.extend(result.grounding_issues)
        if not drafts:
            return None, (), tuple(issues), "; ".join(errors) or None, tuple(failed)
        composed = (
            deterministic_join(drafts)
            if failed
            else self.composer.compose(query, plan, drafts)
        )
        return (
            composed,
            tuple(drafts),
            tuple(issues),
            "; ".join(errors) or None,
            tuple(dict.fromkeys(failed)),
        )

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
    *,
    planner: object | None = None,
    failed_section_ids: tuple[str, ...] = (),
    partial: bool = False,
) -> dict[str, Any]:
    """The teach path's intermediate state, shaped for --debug and for interviews."""
    workspace = evidence_package.evidence_workspace
    depth = getattr(planner, "last_depth_decision", None)
    section_budget = getattr(planner, "last_section_budget", None)
    locate = getattr(planner, "last_locate_selection", None)
    trace = {
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
        "failed_section_ids": list(failed_section_ids),
        "partial_answer": partial,
        "writer_reasoning_reserve": {
            "tokens": WRITER_REASONING_RESERVE_TOKENS,
            "estimated": False,
            "reason": "rounded reasoning_tokens p95 from 64 live high-effort writer calls",
        },
    }
    if depth is not None:
        trace["depth_decision"] = depth.to_dict()
    if section_budget is not None:
        trace["section_budget"] = section_budget.to_dict()
    if locate is not None:
        trace["locate"] = {
            "locate_requirement_ids": list(locate.requirement_ids),
            "locate_evidence_labels": list(locate.evidence_labels),
            "locate_fast_path_used": locate.used,
            "locate_fast_path_skip_reason": locate.skip_reason,
        }
    return trace


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
    *,
    revision: bool = False,
    revision_notes_count: int = 0,
    success: bool = True,
    error: str | None = None,
    call_ids: tuple[int, ...] = (),
    attempt_count: int = 1,
    started_offset_ms: float = 0.0,
) -> SectionExecutionTrace:
    """Build, record and return the cost of generating one section."""
    recorder = current()
    ended_offset_ms = recorder.offset_ms() if recorder is not None else 0.0
    calls = []
    if recorder is not None and call_ids:
        calls = [
            call
            for call_id in call_ids
            if (call := recorder.llm_call(call_id)) is not None
        ]
    input_tokens = None
    output_tokens = None
    if calls:
        known_inputs = [call.input_tokens for call in calls if call.input_tokens is not None]
        known_outputs = [call.output_tokens for call in calls if call.output_tokens is not None]
        input_tokens = sum(known_inputs) if known_inputs else None
        output_tokens = sum(known_outputs) if known_outputs else None
    trace = SectionExecutionTrace(
        section_id=section.id,
        title=section.title,
        section_type=section.section_type,
        latency_ms=0.0 if started is None else (time.perf_counter() - started) * 1000,
        started_offset_ms=started_offset_ms,
        ended_offset_ms=ended_offset_ms,
        evidence_count=len(context.items),
        context_chars=context.total_chars,
        target_tokens=section.target_tokens,
        revision=revision,
        revision_notes_count=revision_notes_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        success=success,
        error=error,
        llm_call_ids=call_ids,
        attempt_count=attempt_count,
    )
    record_section_execution(trace)
    return trace


def _is_transient_failure(exception: Exception, detail: str) -> bool:
    status = getattr(exception, "status_code", None)
    if status == 429 or (isinstance(status, int) and 500 <= status <= 599):
        return True
    text = f"{type(exception).__name__}: {detail}".casefold()
    return any(fragment in text for fragment in (
        "timeout", "timed out", "http 429", "http 5", "curl",
        "connection", "temporarily unavailable",
    ))


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
