from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol

from devcontext.deadline import bounded_timeout, remaining_seconds, RequestDeadlineExceeded
from devcontext.delivery import Generation
from devcontext.execution import execution_scope
from devcontext.llm.streaming import StreamFailure
from devcontext.models import AnswerResult
from devcontext.observability import llm_stage

from .inputs import AnswerInput
from .markdown import MarkdownParts
from .prompts import writer_messages


@dataclass(frozen=True, slots=True)
class AnswerSynthesisResult:
    answer: AnswerResult
    sections: tuple
    answer_input: AnswerInput
    trace: dict


class AnswerSynthesizer(Protocol):
    def synthesize(self, request, answer_input, *, on_event=None, on_section=None, request_started=None) -> AnswerSynthesisResult: ...


class ContinuousAnswerSynthesizer:
    def __init__(self, client_factory, *, max_attempts=2):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 2:
            raise ValueError("Writer attempts must be one or two")
        self.client_factory, self.max_attempts = client_factory, max_attempts
        self.last_client = None

    def synthesize(self, request, answer_input, *, on_event=None, on_section=None, request_started=None):
        # An explicit subscription is request-local. Omitting it retains the
        # application execution_scope already installed by AskService.
        if on_event is not None:
            from devcontext.execution import current_cancellation
            with execution_scope(current_cancellation(), on_event=on_event):
                return self._write(request, answer_input, on_section, request_started)
        return self._write(request, answer_input, on_section, request_started)

    def _write(self, request, answer_input, on_section, request_started):
        started = time.perf_counter()
        request_started = started if request_started is None else request_started
        messages = writer_messages(answer_input)
        attempts, totals, missing = [], {}, set()
        first_content = None
        status, error, finish_reason = "failed", None, None
        for attempt in range(self.max_attempts):
            generation = Generation("continuous")
            parts = MarkdownParts(generation, answer_input.available_labels, on_section)
            iterator = None
            attempt_started = time.perf_counter()
            usage, error, retryable, finish_reason = {}, None, False, None
            self.last_client = None
            try:
                remaining_seconds()
                self.last_client = self.client_factory()
                self.last_client.max_tokens = answer_input.budget["max_output_tokens"]
                if hasattr(self.last_client, "timeout_seconds"):
                    self.last_client.timeout_seconds = bounded_timeout(self.last_client.timeout_seconds)
                finished = False
                with llm_stage("teaching_draft", "continuous"):
                    iterator = self.last_client.generate_stream(messages)
                    for event in iterator:
                        remaining_seconds()
                        if event.type == "content":
                            if finished:
                                raise StreamFailure("content after finish", retryable=False)
                            if event.text.strip() and first_content is None:
                                first_content = (time.perf_counter()-request_started)*1000
                            parts.feed(event.text)
                        elif event.type == "usage":
                            usage.update(event.usage or {})
                        elif event.type == "finish":
                            finish_reason = event.finish_reason
                            if finished or finish_reason != "stop":
                                raise StreamFailure(f"abnormal finish: {finish_reason}", retryable=False)
                            finished = True
                        elif event.type == "error":
                            raise StreamFailure(event.text, retryable=event.retryable)
                        else:
                            raise StreamFailure("unknown stream event", retryable=False)
                    if not finished:
                        raise StreamFailure("missing stream finish", retryable=False)
                    parts.finish()
                    if not parts.sections:
                        raise StreamFailure("empty answer", retryable=False)
                status = "complete"
            except Exception as exc:
                error = str(exc)
                status = "partial" if parts.visible or parts.sections else "failed"
                retryable = not isinstance(exc, RequestDeadlineExceeded) and getattr(exc, "retryable", False)
            finally:
                if iterator is not None and hasattr(iterator, "close"):
                    with llm_stage("teaching_draft", "continuous"):
                        iterator.close()
                counts = dict(getattr(self.last_client, "last_usage", {}))
                counts.update(usage)
                for key in ("prompt_tokens", "completion_tokens", "reasoning_tokens"):
                    if type(counts.get(key)) is int:
                        totals[key] = totals.get(key, 0) + counts[key]
                    else:
                        missing.add(key)
                attempts.append({"attempt": attempt+1, "generation_id": generation.id,
                    "elapsed_ms": (time.perf_counter()-attempt_started)*1000,
                    "error": error, "finish_reason": finish_reason, "usage": counts})
            generation.finish(status)
            if status == "complete" or parts.visible or parts.sections or not retryable:
                break
        labels = list(dict.fromkeys(c for s in parts.sections for c in s.citations))
        trace = {"generation_mode": "continuous", "synthesis_mode": "continuous", "delivery_path": "continuous",
            "completion_status": status, "error": error, "finish_reason": finish_reason,
            "sections_emitted": len(parts.sections), "stream_partial": status == "partial",
            "stream_retry_count": len(attempts)-1, "attempts": attempts,
            "first_content_token_ms": first_content, "warnings": parts.warnings,
            "first_section_ready_ms": None if parts.first_ready_at is None else (parts.first_ready_at-request_started)*1000,
            "stream_started_offset_ms": (started-request_started)*1000,
            "unfinished_text": parts.body + parts.prefix,
            "answer_input": answer_input.to_dict(), "input_tokens": None if "prompt_tokens" in missing else totals.get("prompt_tokens"),
            "output_tokens": None if "completion_tokens" in missing else totals.get("completion_tokens"),
            "reasoning_tokens": None if "reasoning_tokens" in missing else totals.get("reasoning_tokens"),
            "known_usage": totals, "usage_complete": not missing,
            "elapsed_ms": (time.perf_counter()-started)*1000}
        return AnswerSynthesisResult(AnswerResult("\n\n".join(s.markdown for s in parts.sections), labels,
            zero_valid_citation=not labels), tuple(parts.sections), answer_input, trace)
