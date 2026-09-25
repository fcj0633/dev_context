from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from devcontext.models import AnswerResult, ContextBundle, ContextItem
from devcontext.routing import QueryType, RouteDecision


@dataclass(frozen=True, slots=True)
class MissingAspect:
    source_type: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SufficiencyResult:
    enough: bool
    missing_aspects: tuple[MissingAspect, ...]
    reason: str
    decision_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "enough": self.enough,
            "missing_aspects": [aspect.to_dict() for aspect in self.missing_aspects],
            "reason": self.reason,
            "decision_source": self.decision_source,
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


@dataclass(slots=True)
class AgenticTrace:
    route: RouteDecision
    rounds: list[AgenticRoundTrace]
    retry_count: int
    final_sufficiency: SufficiencyResult
    stop_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.to_dict(),
            "rounds": [round_trace.to_dict() for round_trace in self.rounds],
            "retry_count": self.retry_count,
            "final_sufficiency": self.final_sufficiency.to_dict(),
            "stop_reason": self.stop_reason,
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
