from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# Every record here is child diagnostic data. ``StageUsage`` is the only thing
# that partitions the request's wall clock; summing any of these alongside it
# double-counts the same seconds.


@dataclass(frozen=True, slots=True)
class LLMCallTrace:
    """One call to the LLM API.

    Token counts are the provider's own usage numbers, never a character-based
    estimate. Estimates, where a stage has nothing better, are named
    ``*_estimated`` and are never mixed into these fields.

    ``success`` reports the API call itself. ``wasted`` is separate and means the
    call completed but its output never reached the answer - a planner whose
    output failed validation, a composer whose citations were rejected.
    """

    call_id: int
    stage: str
    latency_ms: float
    substage: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    json_mode: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    finish_reason: str | None = None
    success: bool = True
    error: str | None = None
    wasted: bool = False
    wasted_reason: str | None = None

    @property
    def discarded_latency_ms(self) -> float:
        """Latency that produced nothing usable, whether by failure or by discard."""
        return self.latency_ms if (not self.success or self.wasted) else 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["discarded_latency_ms"] = self.discarded_latency_ms
        return value


@dataclass(frozen=True, slots=True)
class EmbeddingCallTrace:
    """One embedding request. ``query`` is the exact text embedded, so repeats
    of the same string inside one request are visible by inspection."""

    call_id: int
    query: str
    latency_ms: float
    batch_size: int = 1
    model: str | None = None
    dimensions: int | None = None
    transport: str | None = None
    success: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RetrievalActionTrace:
    """One search action, with the retrieval pipeline split into its parts.

    The split answers "was this slow in the embedding API, the vector SQL, the
    keyword SQL, or the Python fusion?" rather than leaving one opaque total.
    """

    action_id: str
    requirement_id: str
    round_index: int
    query: str
    source_scope: str
    top_k: int
    total_ms: float
    result_count: int = 0
    query_embedding_ms: float | None = None
    keyword_sql_ms: float | None = None
    vector_sql_ms: float | None = None
    fusion_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SectionExecutionTrace:
    """Per-section generation cost.

    Section-shaped data belongs here and not on ``StageUsage``: that record is
    stage-granular and feeds the wall-clock budget, while these are children of
    the ``teaching_draft`` stage and are counted inside it, never beside it.
    """

    section_id: str
    title: str
    section_type: str
    latency_ms: float
    evidence_count: int = 0
    context_chars: int = 0
    target_tokens: int | None = None
    revision: bool = False
    revision_notes_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    success: bool = True
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PerfRecorder:
    """The collected traces for one request."""

    llm_calls: list[LLMCallTrace] = field(default_factory=list)
    embedding_calls: list[EmbeddingCallTrace] = field(default_factory=list)
    retrieval_actions: list[RetrievalActionTrace] = field(default_factory=list)
    section_executions: list[SectionExecutionTrace] = field(default_factory=list)
    graph_expansions: list[Any] = field(default_factory=list)

    def repeated_embeddings(self) -> dict[str, int]:
        """Queries embedded more than once in this request, with their counts.

        A mixed-scope search embeds the same query once for CODE and once for
        DOCUMENT, so this is expected to be non-empty rather than a bug - the
        point is to price it.
        """
        counts: dict[str, int] = {}
        for call in self.embedding_calls:
            counts[call.query] = counts.get(call.query, 0) + 1
        return {query: count for query, count in counts.items() if count > 1}

    def llm_totals(self) -> dict[str, Any]:
        return {
            "call_count": len(self.llm_calls),
            "latency_ms": sum(call.latency_ms for call in self.llm_calls),
            "discarded_latency_ms": sum(
                call.discarded_latency_ms for call in self.llm_calls
            ),
            "input_tokens": sum(
                call.input_tokens or 0 for call in self.llm_calls
            ),
            "output_tokens": sum(
                call.output_tokens or 0 for call in self.llm_calls
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm_calls": [call.to_dict() for call in self.llm_calls],
            "embedding_calls": [call.to_dict() for call in self.embedding_calls],
            "retrieval_actions": [
                action.to_dict() for action in self.retrieval_actions
            ],
            "section_executions": [
                section.to_dict() for section in self.section_executions
            ],
            "graph_expansions": [trace.to_dict() for trace in self.graph_expansions],
        }
