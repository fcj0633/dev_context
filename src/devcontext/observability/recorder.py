from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace

from devcontext.observability.trace import (
    EmbeddingCallTrace,
    LLMCallTrace,
    PerfRecorder,
    RetrievalActionTrace,
    SectionExecutionTrace,
)


# Both are ambient rather than parameters. The LLM client protocol is
# implemented by two dozen test doubles that accept only ``messages``, so
# threading a stage label through the call signature would have broken all of
# them for the sake of a diagnostic.
_active: ContextVar[PerfRecorder | None] = ContextVar(
    "devcontext_perf_recorder", default=None
)
_stage: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "devcontext_perf_stage", default=None
)

UNATTRIBUTED_STAGE = "unattributed"


def current() -> PerfRecorder | None:
    """The active recorder, or None when this request is not being profiled."""
    return _active.get()


@contextmanager
def capture(recorder: PerfRecorder | None = None) -> Iterator[PerfRecorder]:
    """Collect call-level traces for the duration of the block.

    Outside a capture block every hook below is a no-op, so the ordinary path
    behaves exactly as it did before this module existed.
    """
    active = recorder if recorder is not None else PerfRecorder()
    token = _active.set(active)
    try:
        yield active
    finally:
        _active.reset(token)


@contextmanager
def llm_stage(stage: str, substage: str | None = None) -> Iterator[None]:
    """Label the LLM calls made inside the block."""
    token = _stage.set((stage, substage))
    try:
        yield
    finally:
        _stage.reset(token)


def current_stage() -> tuple[str, str | None]:
    return _stage.get() or (UNATTRIBUTED_STAGE, None)


def record_llm_call(
    *,
    latency_ms: float,
    stage: str | None = None,
    substage: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    reasoning_tokens: int | None = None,
    finish_reason: str | None = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    recorder = _active.get()
    if recorder is None:
        return
    ambient_stage, ambient_substage = current_stage()
    recorder.llm_calls.append(
        LLMCallTrace(
            call_id=len(recorder.llm_calls) + 1,
            stage=stage or ambient_stage,
            substage=substage if substage is not None else ambient_substage,
            latency_ms=latency_ms,
            model=model,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            json_mode=json_mode,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            finish_reason=finish_reason,
            success=success,
            error=error,
        )
    )


def mark_last_call_wasted(reason: str, *, stage: str | None = None) -> None:
    """Flag the most recent call whose completed output never reached the answer.

    Used where the call itself succeeded but the result was rejected afterwards -
    a planner whose JSON failed validation, a composer that invented a citation.

    ``stage`` guards against attributing the discard to an unrelated earlier
    call: a caller that raises before it ever reaches the API would otherwise
    mark whatever ran last.
    """
    recorder = _active.get()
    if recorder is None or not recorder.llm_calls:
        return
    last = recorder.llm_calls[-1]
    if last.wasted:
        return
    if stage is not None and last.stage != stage:
        return
    recorder.llm_calls[-1] = replace(last, wasted=True, wasted_reason=reason)


def record_embedding_call(
    *,
    query: str,
    latency_ms: float,
    batch_size: int = 1,
    model: str | None = None,
    dimensions: int | None = None,
    transport: str | None = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    recorder = _active.get()
    if recorder is None:
        return
    recorder.embedding_calls.append(
        EmbeddingCallTrace(
            call_id=len(recorder.embedding_calls) + 1,
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


def record_retrieval_action(action: RetrievalActionTrace) -> None:
    recorder = _active.get()
    if recorder is None:
        return
    recorder.retrieval_actions.append(action)


def record_section_execution(section: SectionExecutionTrace) -> None:
    recorder = _active.get()
    if recorder is None:
        return
    recorder.section_executions.append(section)
