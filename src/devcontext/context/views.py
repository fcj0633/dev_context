from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from devcontext.context.budget import TokenBudgetPolicy
from devcontext.context.builder import ContextBuilder
from devcontext.context.estimator import TokenEstimator
from devcontext.context.workspace import EvidenceRef, EvidenceWorkspace
from devcontext.models import ContextBundle, ContextItem

DEFAULT_VIEW_MAX_CHARS = 28_000


def context_item_from_ref(ref: EvidenceRef) -> ContextItem:
    """Project a workspace ref into the shape the existing consumers expect.

    The label is the ref's stable evidence id, so a chunk keeps one name no
    matter which view or section is looking at it.
    """
    return ContextItem(
        citation=ref.citation,
        content=ref.content or "",
        chunk_id=ref.chunk_id,
        chunk_type=ref.chunk_type,
        score=ref.retrieval_score,
        retrieval_rank=ref.retrieval_rank,
        truncated=False,
        source_role=ref.source_role,
        temporal_status=ref.temporal_status,
        authority_priority=ref.authority_priority,
        sub_question_ids=list(ref.requirement_ids),
    )


class CoverageView(Protocol):
    """Per-requirement evidence, with no prompt budget applied.

    Implementations must not drop evidence to fit a prompt: coverage judging the
    answer's context is the defect this exists to remove.
    """

    def items_for(self, requirement_id: str) -> Sequence[ContextItem]: ...


@dataclass(slots=True)
class WorkspaceCoverageView:
    workspace: EvidenceWorkspace
    # Which round this check belongs to. Evidence a later round has not found yet
    # must never influence an earlier round's verdict, so it is filtered out here
    # rather than relying on the caller's timing.
    round_index: int | None = None

    def items_for(self, requirement_id: str) -> tuple[ContextItem, ...]:
        refs = self.workspace.for_requirement(requirement_id)
        if self.round_index is not None:
            refs = tuple(ref for ref in refs if self.workspace._chunk_ownership_round.get((requirement_id, ref.chunk_id), ref.first_seen_round) <= self.round_index)
        return tuple(context_item_from_ref(ref) for ref in refs)


@dataclass(slots=True)
class PromptContextView:
    """One prompt's worth of evidence, sliced out of the whole workspace."""

    items: tuple[ContextItem, ...]
    rendered_text: str
    estimated_tokens: int
    budget_tokens: int
    max_chars: int
    omitted_evidence_ids: tuple[str, ...] = ()
    truncated_evidence_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    query: str = ""

    def to_bundle(self) -> "ContextBundle":
        """The same view in the shape the answer writers already consume."""
        return ContextBundle(
            query=self.query,
            items=list(self.items),
            rendered_text=self.rendered_text,
            total_chars=len(self.rendered_text),
            max_chars=self.max_chars,
            truncated=bool(self.omitted_evidence_ids or self.truncated_evidence_ids),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_count": len(self.items),
            "estimated_tokens": self.estimated_tokens,
            "budget_tokens": self.budget_tokens,
            "max_chars": self.max_chars,
            "omitted_evidence_ids": list(self.omitted_evidence_ids),
            "truncated_evidence_ids": list(self.truncated_evidence_ids),
            "evidence_ids": list(self.evidence_ids),
            "truncated": bool(self.omitted_evidence_ids or self.truncated_evidence_ids),
        }


def build_context_view(
    workspace: EvidenceWorkspace,
    requirement_ids: Iterable[str] = (),
    *,
    evidence_labels: Iterable[str] = (),
    query: str = "",
    max_chars: int | None = None,
    policy: TokenBudgetPolicy | None = None,
    estimator: TokenEstimator | None = None,
    fixed_tokens: int = 0,
    requested_output_tokens: int = 0,
) -> PromptContextView:
    """Build a budgeted view over the evidence bound to these requirements.

    This is the one builder the planner, a single answer section and the reviewer
    all use - they differ only in which requirement ids they pass, so there is no
    reason for three near-identical classes.

    Pass a ``policy`` to size the view from the model's token window, or
    ``max_chars`` to size it from a character ceiling. The token path is the one
    that is tied to something real about the model; the character path remains
    for callers that have no model to ask.
    """
    refs = (
        workspace.by_citation(evidence_labels)
        if evidence_labels
        else workspace.for_requirements(requirement_ids)
    )
    items = [context_item_from_ref(ref) for ref in refs]

    if max_chars is not None and max_chars < 1:
        # A window too small to hold anything is a legal outcome of budgeting,
        # not an error: it means this model cannot carry this prompt.
        return PromptContextView(
            items=(),
            rendered_text="",
            estimated_tokens=0,
            budget_tokens=0,
            max_chars=0,
            omitted_evidence_ids=tuple(ref.evidence_id for ref in refs),
            evidence_ids=(),
            query=query,
        )

    if policy is not None:
        if estimator is None:
            raise ValueError("a token budget needs an estimator to size characters")
        decision = policy.decide(
            fixed_tokens=fixed_tokens,
            requested_output_tokens=requested_output_tokens,
        )
        budget_tokens = decision.context_budget_tokens
        # allowed with a zero remainder means the fixed prompt and the reserve
        # consume the window exactly; nothing is left for evidence.
        if not decision.allowed or budget_tokens < 1:
            return PromptContextView(
                items=(),
                rendered_text="",
                estimated_tokens=0,
                budget_tokens=0,
                max_chars=0,
                omitted_evidence_ids=tuple(ref.evidence_id for ref in refs),
                evidence_ids=(),
                query=query,
            )
        max_chars = policy.context_chars_for(decision, estimator)
    else:
        budget_tokens = 0
        if max_chars is None:
            max_chars = DEFAULT_VIEW_MAX_CHARS

    rendered, kept, _ = ContextBuilder(max_chars=max_chars).render_items(items)
    kept_ids = {item.chunk_id for item in kept}
    truncated_ids = {item.chunk_id for item in kept if item.truncated}

    return PromptContextView(
        items=tuple(kept),
        rendered_text=rendered,
        estimated_tokens=estimator.estimate(rendered) if estimator else 0,
        budget_tokens=budget_tokens,
        max_chars=max_chars,
        omitted_evidence_ids=tuple(
            ref.evidence_id for ref in refs if ref.chunk_id not in kept_ids
        ),
        truncated_evidence_ids=tuple(
            ref.evidence_id for ref in refs if ref.chunk_id in truncated_ids
        ),
        evidence_ids=tuple(item.citation.label for item in kept),
        query=query,
    )
