from __future__ import annotations

import time
from types import SimpleNamespace
from devcontext.deadline import bounded_timeout, remaining_seconds
from devcontext.explanation.grounding import grounding_issues
from devcontext.explanation.stream_writer import SectionParser, validate_section
from devcontext.explanation.v3.prompts import writer_messages
from devcontext.llm.streaming import StreamFailure
from devcontext.observability import llm_stage, mark_last_call_wasted


class TeachingWriterV3:
    def __init__(self, client_factory, *, permissive=False):
        self.client_factory = client_factory
        self.permissive = permissive
        self.last_client = None

    def write(self, question, blueprint, pack, budget, *, request_started, on_section=None, messages=None):
        messages = messages or writer_messages(question, blueprint, pack, universal=self.permissive)
        available = {e["label"] for e in pack.catalog}
        sections, attempts = [], []
        first_ready = first_content = None
        failed_section = error = None
        finish_reason = None
        status = "failed"
        totals = {}
        grounding_warnings = []
        rejected_section = None
        rejected_protocol_buffer = None
        for attempt in range(2):
            parser = SectionParser(s.id for s in pack.sections)
            iterator = None
            started = time.perf_counter()
            usage = {}
            error = None
            try:
                self.last_client = self.client_factory()
                if hasattr(self.last_client, "max_tokens"):
                    self.last_client.max_tokens = budget.max_output_tokens
                if hasattr(self.last_client, "timeout_seconds"):
                    self.last_client.timeout_seconds = bounded_timeout(self.last_client.timeout_seconds)
                finished = False
                with llm_stage("teaching_draft", "v3"):
                    iterator = self.last_client.generate_stream(messages)
                    for event in iterator:
                        remaining_seconds()
                        if event.type == "content":
                            if finished:
                                raise StreamFailure("content after finish")
                            if event.text.strip() and first_content is None:
                                first_content = (time.perf_counter()-request_started)*1000
                            for section_id, body in parser.feed(event.text):
                                failed_section = section_id
                                try:
                                    valid = validate_section(pack.sections[len(sections)], body, available,
                                        permissive=self.permissive, warnings=grounding_warnings)
                                except Exception:
                                    rejected_section = {"id": section_id, "markdown": body}
                                    raise
                                contract = pack.sections[len(sections)].contract
                                local_claims = [*contract["owned_claims"], *contract["may_reference"], *contract["dependency_claims"]]
                                adapter = SimpleNamespace(
                                    id=section_id, evidence_labels=pack.sections[len(sections)].evidence_labels,
                                    claim_plans=[SimpleNamespace(claim_type=c["claim_type"],
                                        conditional=bool(c["preconditions"]) or c["claim_type"] == "ILLUSTRATIVE_EXAMPLE") for c in local_claims],
                                    evidence_state="UNVERIFIED" if local_claims and all(c["status"] == "UNKNOWN" for c in local_claims) else "PARTIAL",
                                )
                                local_warnings = [issue.to_dict() for issue in grounding_issues(adapter, body)]
                                if on_section is not None:
                                    try:
                                        on_section(valid)
                                    except Exception as exc:
                                        raise StreamFailure("section callback failed", retryable=False) from exc
                                sections.append(valid)
                                grounding_warnings.extend(local_warnings)
                                failed_section = None
                                if first_ready is None:
                                    first_ready = (time.perf_counter()-request_started)*1000
                        elif event.type == "usage":
                            usage.update(event.usage or {})
                        elif event.type == "finish":
                            finish_reason = event.finish_reason
                            if finished or finish_reason != "stop":
                                raise StreamFailure(f"abnormal finish: {finish_reason}")
                            finished = True
                        elif event.type == "error":
                            raise StreamFailure(event.text, retryable=event.retryable)
                        else:
                            raise StreamFailure("unknown stream event")
                    if not finished:
                        raise StreamFailure("missing stream finish")
                    parser.finish()
                status = "complete"
                retryable = False
            except Exception as exc:
                error = str(exc)
                rejected_protocol_buffer = parser.buffer[:4000]
                failed_section = failed_section or parser.next_id
                status = "partial" if sections else "failed"
                retryable = getattr(exc, "retryable", False)
            finally:
                if iterator is not None and hasattr(iterator, "close"):
                    with llm_stage("teaching_draft", "v3"):
                        iterator.close()
                client_usage = dict(getattr(self.last_client, "last_usage", {}))
                client_usage.update(usage)
                for key, count in client_usage.items():
                    if type(count) is int:
                        totals[key] = totals.get(key, 0) + count
                attempts.append({"attempt": attempt+1, "elapsed_ms": (time.perf_counter()-started)*1000, "error": error, "usage": client_usage})
            if status == "complete" or sections or attempt or not retryable:
                break
            try:
                remaining = remaining_seconds()
            except TimeoutError:
                break
            if remaining is not None and remaining < 60:
                break
            mark_last_call_wasted("V3 stream rejected before publication", stage="teaching_draft")
        trace = {"generation_mode": "v3", "completion_status": status, "error": error,
                 "failed_section": failed_section, "sections_planned": len(pack.sections), "sections_emitted": len(sections),
                 "missing_sections": [s.id for s in pack.sections[len(sections):]], "stream_partial": status == "partial",
                 "stream_retry_count": len(attempts)-1, "first_content_token_ms": first_content,
                 "first_section_ready_ms": first_ready, "finish_reason": finish_reason, "attempts": attempts,
                 "input_tokens": totals.get("prompt_tokens"), "output_tokens": totals.get("completion_tokens"),
                 "reasoning_tokens": totals.get("reasoning_tokens")}
        trace["grounding_warnings"] = grounding_warnings
        if rejected_protocol_buffer is not None:
            trace["rejected_protocol_buffer"] = rejected_protocol_buffer
        if rejected_section is not None:
            trace["rejected_section"] = rejected_section
        return tuple(sections), trace
