"""One buffered Markdown delivery using existing evidence, without a blueprint."""
import json
import re
import time

from devcontext.deadline import bounded_timeout, remaining_seconds
from devcontext.explanation.stream_writer import ValidatedSection
from devcontext.explanation.v3.prompts import WRITER_INSTRUCTIONS
from devcontext.explanation.v3.universal import WRITING_METHOD
from devcontext.llm.client import LLMMessage
from devcontext.llm.streaming import StreamFailure
from devcontext.observability import llm_stage


def direct_messages(request, package, pack, capabilities, estimator):
    # Reuse the current writing/boundary guidance; remove blueprint-only instructions.
    teaching = WRITER_INSTRUCTIONS.split("【计划与协议】", 1)[0]
    teaching = "\n".join(line for line in teaching.splitlines() if not any(word in line for word in
                          ("Owned", "checkpoint", "scenario_setup", "Claim/Goal/Delta", "理解检查", "逐节契约")))
    system = teaching + "\n" + WRITING_METHOD + "\n直接输出普通 Markdown 正文，不输出章节协议标记或内部规划字段。"
    payload = {"question": request.original_query, "reader_assumption":
               request.policy.reader_assumption if request.policy else "Java 基础学习者",
               "retrieval_gaps": [getattr(r, "requirement_id", str(r)) for r in getattr(package, "requirement_coverage", ())
                                  if getattr(r, "status", None) not in {"SATISFIED", None}], "evidence": []}
    source = list(pack.catalog) if pack else [dict(label=i.citation.label, citation=i.citation.to_dict(),
                  content=i.content, source_role=i.source_role, temporal_status=i.temporal_status, truncated=i.truncated)
                  for i in package.context_bundle.items]
    output = min(32768, capabilities.max_output_tokens)
    omitted, seen = [], set()
    def messages():
        return [LLMMessage("system", system), LLMMessage("user", json.dumps(payload, ensure_ascii=False))]
    def fits():
        return sum(estimator.estimate(m.content) for m in messages()) + output + 1024 <= capabilities.context_window
    if not fits():
        raise ValueError("正文兜底基本输入与输出预留超过模型窗口")
    for entry in source:
        label = entry["label"]
        if label in seen:
            continue
        seen.add(label)
        if not entry.get("content", "").strip() or entry.get("truncated"):
            omitted.append(label)
            continue
        payload["evidence"].append(entry)
        if not fits():
            payload["evidence"].pop()
            omitted.append(label)
    return messages(), {e["label"] for e in payload["evidence"]}, omitted, output


def write_direct(client_factory, request, package, pack, capabilities, estimator, *, started, on_section):
    messages, allowed, omitted, output = direct_messages(request, package, pack, capabilities, estimator)
    client, iterator = None, None
    parts, usage = [], {}
    finished = False
    finish_reason = error = None
    first_content = None
    call_started = time.perf_counter()
    try:
        remaining_seconds()
        client = client_factory()
        if hasattr(client, "timeout_seconds"):
            client.timeout_seconds = bounded_timeout(client.timeout_seconds)
        if hasattr(client, "max_tokens"):
            client.max_tokens = output
        with llm_stage("teaching_fallback_draft"):
            iterator = client.generate_stream(messages)
            for event in iterator:
                remaining_seconds()
                if event.type == "content":
                    if finished:
                        raise StreamFailure("content after finish")
                    parts.append(event.text)
                    if event.text.strip() and first_content is None:
                        first_content = (time.perf_counter() - started) * 1000
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
    except Exception as exc:
        error = str(exc)
    finally:
        if iterator is not None and hasattr(iterator, "close"):
            with llm_stage("teaching_fallback_draft"):
                iterator.close()
    warnings = []
    def cite(match):
        label = match[1]
        if label in allowed:
            return match[0]
        warnings.append({"warning": "invalid citation removed", "reference": label})
        return ""
    body = re.sub(r"\[([EC]\d[^\]\n]*)\]", cite, "".join(parts)).strip()
    status = "complete" if finished and not error and body else ("partial" if body else "failed")
    if not body and not error:
        error = "empty fallback body"
    sections = ()
    first_ready = None
    if body:
        section = ValidatedSection("S1", body, tuple(dict.fromkeys(re.findall(r"\[(E[1-9]\d*)\]", body))))
        try:
            if on_section:
                on_section(section)
            sections = (section,)
            first_ready = (time.perf_counter() - started) * 1000
        except Exception:
            status, error = "failed", "section callback failed"
    return sections, {"generation_mode": "v3", "completion_status": status, "error": error,
        "delivery_path": "full_direct_fallback", "sections_planned": 1, "sections_emitted": len(sections),
        "missing_sections": [] if sections else ["S1"], "stream_partial": status == "partial",
        "first_content_token_ms": first_content, "first_section_ready_ms": first_ready,
        "finish_reason": finish_reason, "grounding_warnings": warnings, "omitted_evidence_labels": omitted,
        "attempts": [{"attempt": 1, "elapsed_ms": (time.perf_counter()-call_started)*1000,
                      "error": error, "usage": usage, "model": getattr(client, "model", None)}]}, client
