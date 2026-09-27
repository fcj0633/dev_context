from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from devcontext.answer import format_source
from devcontext.models import AnswerResult, ContextBundle, ContextItem
from devcontext.routing import QueryType, RouteDecision


@dataclass(frozen=True, slots=True)
class MissingAspect:
    source_type: str
    description: str
    # Empty on the legacy path, which has no evidence requirements to name.
    requirement_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvidenceStatus:
    requirement_id: str
    satisfied: bool
    reason: str

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "retrieval_query": self.retrieval_query,
            "retrieval_query_type": self.retrieval_query_type.value,
            "selected_chunks": [chunk.to_dict() for chunk in self.selected_chunks],
            "sufficiency": self.sufficiency.to_dict(),
            "rewrite": self.rewrite.to_dict() if self.rewrite else None,
            "rewrite_error": self.rewrite_error,
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
    evidence_plan: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
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
            "evidence_plan": self.evidence_plan,
        }


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


@dataclass(slots=True)
class AgenticAnswerResult:
    answer_result: AnswerResult
    context_bundle: ContextBundle
    trace: AgenticTrace

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_result": self.answer_result.to_dict(),
            "context_bundle": self.context_bundle.to_dict(),
            "trace": self.trace.to_dict(),
        }
