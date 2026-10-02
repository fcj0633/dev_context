from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field, replace
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
    started_offset_ms: float = 0.0
    ended_offset_ms: float = 0.0
    substage: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    json_mode: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    visible_output_tokens: int | None = None
    token_detail_available: bool = False
    finish_reason: str | None = None
    parallel_group_id: str | None = None
    attempt_index: int = 1
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
    started_offset_ms: float = 0.0
    ended_offset_ms: float = 0.0
    evidence_count: int = 0
    context_chars: int = 0
    target_tokens: int | None = None
    revision: bool = False
    revision_notes_count: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    success: bool = True
    error: str | None = None
    llm_call_ids: tuple[int, ...] = ()
    attempt_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["llm_call_ids"] = list(self.llm_call_ids)
        return value


@dataclass(slots=True)
class PerfRecorder:
    """The collected traces for one request."""

    request_started_at: float = field(default_factory=time.perf_counter)
    llm_calls: list[LLMCallTrace] = field(default_factory=list)
    embedding_calls: list[EmbeddingCallTrace] = field(default_factory=list)
    retrieval_actions: list[RetrievalActionTrace] = field(default_factory=list)
    section_executions: list[SectionExecutionTrace] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next_llm_call_id: int = field(default=1, repr=False)
    _next_embedding_call_id: int = field(default=1, repr=False)

    def start_request(self) -> None:
        """Start the monotonic request clock used by every child trace."""
        with self._lock:
            self.request_started_at = time.perf_counter()

    def offset_ms(self, timestamp: float | None = None) -> float:
        value = time.perf_counter() if timestamp is None else timestamp
        return max(0.0, (value - self.request_started_at) * 1000)

    def begin_llm_call(
        self,
        *,
        stage: str,
        substage: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
        parallel_group_id: str | None = None,
        attempt_index: int = 1,
    ) -> int:
        started = self.offset_ms()
        with self._lock:
            call_id = self._next_llm_call_id
            self._next_llm_call_id += 1
            self.llm_calls.append(
                LLMCallTrace(
                    call_id=call_id,
                    stage=stage,
                    substage=substage,
                    latency_ms=0.0,
                    started_offset_ms=started,
                    ended_offset_ms=started,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                    parallel_group_id=parallel_group_id,
                    attempt_index=max(1, attempt_index),
                    success=False,
                    error="call did not finish",
                )
            )
        return call_id

    def finish_llm_call(
        self,
        call_id: int,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        visible_output_tokens: int | None = None,
        token_detail_available: bool = False,
        finish_reason: str | None = None,
        success: bool = True,
        error: str | None = None,
        latency_ms: float | None = None,
    ) -> None:
        ended = self.offset_ms()
        with self._lock:
            index = self._llm_index(call_id)
            previous = self.llm_calls[index]
            effective_latency = (
                max(0.0, latency_ms)
                if latency_ms is not None
                else max(0.0, ended - previous.started_offset_ms)
            )
            self.llm_calls[index] = replace(
                previous,
                latency_ms=effective_latency,
                ended_offset_ms=ended,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                visible_output_tokens=visible_output_tokens,
                token_detail_available=token_detail_available,
                finish_reason=finish_reason,
                success=success,
                error=error,
            )

    def mark_call_wasted(self, call_id: int, reason: str, *, stage: str | None = None) -> bool:
        with self._lock:
            try:
                index = self._llm_index(call_id)
            except KeyError:
                return False
            call = self.llm_calls[index]
            if call.wasted or (stage is not None and call.stage != stage):
                return False
            self.llm_calls[index] = replace(
                call, wasted=True, wasted_reason=reason
            )
            return True

    def llm_call(self, call_id: int) -> LLMCallTrace | None:
        with self._lock:
            try:
                return self.llm_calls[self._llm_index(call_id)]
            except KeyError:
                return None

    def append_embedding_call(
        self,
        *,
        query: str,
        latency_ms: float,
        batch_size: int = 1,
        model: str | None = None,
        dimensions: int | None = None,
        transport: str | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> int:
        with self._lock:
            call_id = self._next_embedding_call_id
            self._next_embedding_call_id += 1
            self.embedding_calls.append(
                EmbeddingCallTrace(
                    call_id=call_id,
                    query=query,
                    latency_ms=latency_ms,
                    batch_size=batch_size,
                    model=model,
                    dimensions=dimensions,
                    transport=transport,
                    success=success,
                    error=error,
                )
            )
        return call_id

    def append_retrieval_action(self, action: RetrievalActionTrace) -> None:
        with self._lock:
            self.retrieval_actions.append(action)

    def append_section(self, section: SectionExecutionTrace) -> None:
        with self._lock:
            self.section_executions.append(section)

    def _llm_index(self, call_id: int) -> int:
        for index, call in enumerate(self.llm_calls):
            if call.call_id == call_id:
                return index
        raise KeyError(call_id)

    def repeated_embeddings(self) -> dict[str, int]:
        """Queries embedded more than once in this request, with their counts.

        A mixed-scope search embeds the same query once for CODE and once for
        DOCUMENT, so this is expected to be non-empty rather than a bug - the
        point is to price it.
        """
        with self._lock:
            calls = tuple(self.embedding_calls)
        counts: dict[str, int] = {}
        for call in calls:
            counts[call.query] = counts.get(call.query, 0) + 1
        return {query: count for query, count in counts.items() if count > 1}

    def llm_totals(self) -> dict[str, Any]:
        with self._lock:
            calls = tuple(self.llm_calls)
        return {
            "call_count": len(calls),
            "latency_ms": sum(call.latency_ms for call in calls),
            "discarded_latency_ms": sum(
                call.discarded_latency_ms for call in calls
            ),
            "input_tokens": sum(
                call.input_tokens or 0 for call in calls
            ),
            "output_tokens": sum(
                call.output_tokens or 0 for call in calls
            ),
            "reasoning_tokens": sum(
                call.reasoning_tokens or 0 for call in calls
            ),
            "visible_output_tokens": sum(
                call.visible_output_tokens or 0 for call in calls
            ),
            "token_detail_available_calls": sum(
                1 for call in calls if call.token_detail_available
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            llm_calls = tuple(self.llm_calls)
            embedding_calls = tuple(self.embedding_calls)
            retrieval_actions = tuple(self.retrieval_actions)
            section_executions = tuple(self.section_executions)
        return {
            "llm_calls": [call.to_dict() for call in llm_calls],
            "embedding_calls": [call.to_dict() for call in embedding_calls],
            "retrieval_actions": [
                action.to_dict() for action in retrieval_actions
            ],
            "section_executions": [
                section.to_dict() for section in section_executions
            ],
        }
