from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from devcontext.observability.trace import (
    LLMCallTrace,
    PerfRecorder,
    RetrievalActionTrace,
    SectionExecutionTrace,
)


# Ambient state keeps the public LLMClient protocol unchanged. ContextVar state is
# copied explicitly by the section executor before a task enters a worker thread.
_active: ContextVar[PerfRecorder | None] = ContextVar(
    "devcontext_perf_recorder", default=None
)
_stage: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "devcontext_perf_stage", default=None
)
_active_call_id: ContextVar[int | None] = ContextVar(
    "devcontext_active_llm_call_id", default=None
)
_last_completed_call_id: ContextVar[int | None] = ContextVar(
    "devcontext_last_completed_llm_call_id", default=None
)
_parallel_group: ContextVar[str | None] = ContextVar(
    "devcontext_parallel_group", default=None
)
_attempt_index: ContextVar[int] = ContextVar(
    "devcontext_llm_attempt", default=1
)

UNATTRIBUTED_STAGE = "unattributed"


def current() -> PerfRecorder | None:
    return _active.get()


@contextmanager
def capture(recorder: PerfRecorder | None = None) -> Iterator[PerfRecorder]:
    """Collect call-level traces while remaining a no-op outside the block."""
    active = recorder if recorder is not None else PerfRecorder()
    active.start_request()
    token = _active.set(active)
    call_token = _active_call_id.set(None)
    completed_token = _last_completed_call_id.set(None)
    try:
        yield active
    finally:
        _last_completed_call_id.reset(completed_token)
        _active_call_id.reset(call_token)
        _active.reset(token)


@contextmanager
def llm_stage(stage: str, substage: str | None = None) -> Iterator[None]:
    token = _stage.set((stage, substage))
    try:
        yield
    finally:
        _stage.reset(token)


@contextmanager
def parallel_group(group_id: str | None) -> Iterator[None]:
    token = _parallel_group.set(group_id)
    try:
        yield
    finally:
        _parallel_group.reset(token)


@contextmanager
def llm_attempt(index: int) -> Iterator[None]:
    token = _attempt_index.set(max(1, index))
    try:
        yield
    finally:
        _attempt_index.reset(token)


def current_stage() -> tuple[str, str | None]:
    return _stage.get() or (UNATTRIBUTED_STAGE, None)


def current_call_id() -> int | None:
    return _active_call_id.get()


def last_completed_call_id() -> int | None:
    return _last_completed_call_id.get()


def last_completed_call() -> LLMCallTrace | None:
    recorder = _active.get()
    call_id = _last_completed_call_id.get()
    if recorder is None or call_id is None:
        return None
    return recorder.llm_call(call_id)


def begin_llm_call(
    *,
    stage: str | None = None,
    substage: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> int | None:
    recorder = _active.get()
    if recorder is None:
        return None
    ambient_stage, ambient_substage = current_stage()
    call_id = recorder.begin_llm_call(
        stage=stage or ambient_stage,
        substage=substage if substage is not None else ambient_substage,
        model=model,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        json_mode=json_mode,
        parallel_group_id=_parallel_group.get(),
        attempt_index=_attempt_index.get(),
    )
    _active_call_id.set(call_id)
    return call_id


def finish_llm_call(
    call_id: int | None,
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
    recorder = _active.get()
    if recorder is None or call_id is None:
        return
    recorder.finish_llm_call(
        call_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        visible_output_tokens=visible_output_tokens,
        token_detail_available=token_detail_available,
        finish_reason=finish_reason,
        success=success,
        error=error,
        latency_ms=latency_ms,
    )
    _last_completed_call_id.set(call_id)
    if _active_call_id.get() == call_id:
        _active_call_id.set(None)


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
    visible_output_tokens: int | None = None,
    token_detail_available: bool = False,
    finish_reason: str | None = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    """Compatibility helper for non-DeepSeek clients and existing tests."""
    call_id = begin_llm_call(
        stage=stage,
        substage=substage,
        model=model,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
    finish_llm_call(
        call_id,
        latency_ms=latency_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        visible_output_tokens=(
            output_tokens if visible_output_tokens is None else visible_output_tokens
        ),
        token_detail_available=token_detail_available,
        finish_reason=finish_reason,
        success=success,
        error=error,
    )


def mark_call_wasted(
    call_id: int | None, reason: str, *, stage: str | None = None
) -> None:
    recorder = _active.get()
    if recorder is None or call_id is None:
        return
    recorder.mark_call_wasted(call_id, reason, stage=stage)


def mark_last_call_wasted(reason: str, *, stage: str | None = None) -> None:
    """Serial compatibility wrapper; concurrent code must use an exact call id."""
    mark_call_wasted(_last_completed_call_id.get(), reason, stage=stage)


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
    recorder.append_embedding_call(
        query=query,
        latency_ms=latency_ms,
        batch_size=batch_size,
        model=model,
        dimensions=dimensions,
        transport=transport,
        success=success,
        error=error,
    )


def record_retrieval_action(action: RetrievalActionTrace) -> None:
    recorder = _active.get()
    if recorder is not None:
        recorder.append_retrieval_action(action)


def record_section_execution(section: SectionExecutionTrace) -> None:
    recorder = _active.get()
    if recorder is not None:
        recorder.append_section(section)
