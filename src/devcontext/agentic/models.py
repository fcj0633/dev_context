from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from devcontext.answer.generator import format_source
from devcontext.context.registry import EvidenceCatalog
from devcontext.models import AnswerResult, ContextBundle, ContextItem
from devcontext.routing import QueryType, RouteDecision


@dataclass(frozen=True, slots=True)
class StageUsage:
    stage: str
    latency_ms: float
    decision_source: str
    model: str | None = None
    reasoning_effort: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MissingAspect:
    source_type: str
    description: str
    # Empty on the legacy path, which has no sub-questions to name.
    sub_question_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvidenceStatus:
    sub_question_id: str
    satisfied: bool
    reason: str
    # The requirement's coverage state (SATISFIED / PARTIAL / MISSING /
    # UNVERIFIED). Carried so callers stop reducing four states to a boolean:
    # "could not verify" and "found nothing" are not the same claim.
    state: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SufficiencyResult:
    enough: bool
    missing_aspects: tuple[MissingAspect, ...]
    reason: str
    decision_source: str
    statuses: tuple[EvidenceStatus, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "enough": self.enough,
            "missing_aspects": [aspect.to_dict() for aspect in self.missing_aspects],
            "reason": self.reason,
            "decision_source": self.decision_source,
            "statuses": [status.to_dict() for status in self.statuses],
        }


@dataclass(frozen=True, slots=True)
class RewriteResult:
    original_query: str
    rewritten_query: str
    target_query_type: QueryType
    targeted_aspects: tuple[MissingAspect, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "rewritten_query": self.rewritten_query,
            "target_query_type": self.target_query_type.value,
            "targeted_aspects": [
                aspect.to_dict() for aspect in self.targeted_aspects
            ],
        }


@dataclass(frozen=True, slots=True)
class SelectedChunkTrace:
    citation_label: str
    chunk_id: int
    source_type: str
    file_path: str
    identity: str
    retrieval_rank: int
    source_role: str = "UNKNOWN"
    temporal_status: str = "UNKNOWN"
    authority_priority: int = 50
    sub_question_ids: tuple[str, ...] = ()

    @classmethod
    def from_context_item(cls, item: ContextItem) -> "SelectedChunkTrace":
        citation = item.citation
        if citation.source_type == "CODE":
            parts = [
                part
                for part in (citation.class_name, citation.symbol_name)
                if part
            ]
            identity = "#".join(parts) or citation.signature or item.chunk_type
        else:
            identity = " > ".join(citation.heading_path) or item.chunk_type
        return cls(
            citation_label=citation.label,
            chunk_id=item.chunk_id,
            source_type=citation.source_type,
            file_path=citation.file_path,
            identity=identity,
            retrieval_rank=item.retrieval_rank,
            source_role=item.source_role,
            temporal_status=item.temporal_status,
            authority_priority=item.authority_priority,
            sub_question_ids=tuple(item.sub_question_ids),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgenticRoundTrace:
    round_index: int
    retrieval_query: str
    retrieval_query_type: QueryType
    selected_chunks: list[SelectedChunkTrace]
    sufficiency: SufficiencyResult
    rewrite: RewriteResult | None = None
    rewrite_error: str | None = None
    rewrites: list[RewriteResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "retrieval_query": self.retrieval_query,
            "retrieval_query_type": self.retrieval_query_type.value,
            "selected_chunks": [chunk.to_dict() for chunk in self.selected_chunks],
            "sufficiency": self.sufficiency.to_dict(),
            "rewrite": self.rewrite.to_dict() if self.rewrite else None,
            "rewrite_error": self.rewrite_error,
            "rewrites": [rewrite.to_dict() for rewrite in self.rewrites],
        }


@dataclass(frozen=True, slots=True)
class SubQuestionTrace:
    """One planned sub-question and what it retrieved.

    ``selected_chunks`` labels are local to this sub-question's own candidate view
    (``C1``..``Cn``), not the final ContextBundle labels.
    """

    sub_question_id: str
    question: str
    purpose: str
    query_type: str
    selected_chunks: list[SelectedChunkTrace]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_question_id": self.sub_question_id,
            "question": self.question,
            "purpose": self.purpose,
            "query_type": self.query_type,
            "selected_chunks": [chunk.to_dict() for chunk in self.selected_chunks],
        }


@dataclass(slots=True)
class AgenticTrace:
    route: RouteDecision
    rounds: list[AgenticRoundTrace]
    retry_count: int
    final_sufficiency: SufficiencyResult
    stop_reason: str
    plan: dict[str, Any] | None = None
    sub_question_traces: list[SubQuestionTrace] = field(default_factory=list)
    citations: dict[str, Any] | None = None
    sections: dict[str, Any] | None = None
    answer_plan: dict[str, Any] | None = None
    source_conflicts: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] | None = None
    stage_usage: list[StageUsage] = field(default_factory=list)
    evidence_plan: dict[str, Any] | None = None
    requirement_traces: list[dict[str, Any]] = field(default_factory=list)
    search_actions: list[dict[str, Any]] = field(default_factory=list)
    coverage_rounds: list[dict[str, Any]] = field(default_factory=list)
    final_coverage: list[dict[str, Any]] = field(default_factory=list)
    evidence_package_state: str | None = None
    # Only the teach path fills this. Omitted from the dict when absent so the
    # legacy and explain traces stay byte-for-byte what they were.
    explanation_plan: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "route": self.route.to_dict(),
            "rounds": [round_trace.to_dict() for round_trace in self.rounds],
            "retry_count": self.retry_count,
            "final_sufficiency": self.final_sufficiency.to_dict(),
            "stop_reason": self.stop_reason,
            "plan": self.plan,
            "sub_question_traces": [
                trace.to_dict() for trace in self.sub_question_traces
            ],
            "citations": self.citations,
            "sections": self.sections,
            "answer_plan": self.answer_plan,
            "source_conflicts": self.source_conflicts,
            "review": self.review,
            "stage_usage": [usage.to_dict() for usage in self.stage_usage],
        }
        if self.evidence_plan is not None:
            value.update(
                {
                    "evidence_plan": self.evidence_plan,
                    "requirement_traces": self.requirement_traces,
                    "search_actions": self.search_actions,
                    "coverage_rounds": self.coverage_rounds,
                    "final_coverage": self.final_coverage,
                    "evidence_package_state": self.evidence_package_state,
                }
            )
        if self.explanation_plan is not None:
            value["explanation_plan"] = self.explanation_plan
        return value


def build_citation_trace(
    answer_result: AnswerResult, context_bundle: ContextBundle
) -> dict[str, Any]:
    """The only remaining trace of citations, since they are stripped from the answer."""
    citations = {
        item.citation.label: item.citation for item in context_bundle.items
    }
    return {
        "used_labels": list(answer_result.used_citations),
        "invalid_labels": list(answer_result.invalid_citations),
        "zero_valid": answer_result.zero_valid_citation,
        "sources": [
            format_source(citations[label])
            for label in answer_result.used_citations
            if label in citations
        ],
    }


MAX_ERROR_CHARS = 200


def error_detail(exception: BaseException, limit: int = MAX_ERROR_CHARS) -> str:
    """Class name plus message, so a failure is diagnosable from the trace alone.

    Stage code used to record only ``type(exception).__name__``, which made a
    proxy outage indistinguishable from a genuine parse rejection: both showed up
    as ``RuntimeError`` with nothing to tell them apart.
    """
    message = " ".join(str(exception).split())[:limit]
    name = type(exception).__name__
    return f"{name}: {message}" if message else name


@dataclass(slots=True)
class AgenticAnswerResult:
    answer_result: AnswerResult
    # What the answer was actually written from. On the teach path this is the
    # evidence the explanation plan bound, not the whole retrieval context, so
    # the CLI's Sources listing and the citation trace both resolve correctly.
    context_bundle: ContextBundle
    trace: AgenticTrace
    # The full evidence set behind that bundle, with stable ids.
    evidence_catalog: EvidenceCatalog | None = None
    workspace_stats: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_result": self.answer_result.to_dict(),
            "context_bundle": self.context_bundle.to_dict(),
            "trace": self.trace.to_dict(),
        }
