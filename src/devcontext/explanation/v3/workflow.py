from __future__ import annotations

import time
from devcontext.context.budget import TokenBudgetPolicy
from devcontext.deadline import request_deadline, remaining_seconds
from devcontext.explanation.budget import OutputBudget
from devcontext.explanation.v3.errors import UnsupportedQuestionKind
from devcontext.explanation.v3.evidence_organizer import build_writer_evidence_pack
from devcontext.explanation.v3.prompts import writer_messages
from devcontext.models import AnswerResult


def run_v3(host, request, package, *, depth, on_section=None):
    from devcontext.explanation.workflow import TeachingAnswerResult, _stage
    started = getattr(host, "request_started_at", time.perf_counter())
    stages = []
    blueprint = pack = budget = None
    trace = {"generation_mode": "v3", "completion_status": "failed", "sections_emitted": 0, "stream_partial": False}
    sections = ()
    deadline_seconds = getattr(host, "request_timeout_seconds", 180)
    with request_deadline(deadline_seconds, started=started):
        try:
            remaining_seconds()
            if host.v3_planner is None or host.v3_writer is None:
                raise ValueError("V3 requires planner and writer")
            t = time.perf_counter()
            try:
                blueprint = host.v3_planner.plan(request, package, depth=depth)
            finally:
                stages.append(_stage("teaching_planning_v3", t, "llm", host.v3_planner.last_client, "high"))
            t = time.perf_counter()
            try:
                pack = build_writer_evidence_pack(blueprint, package,
                    required_token_budget=host.capabilities.context_window, estimator=host.estimator)
            finally:
                stages.append(_stage("teaching_evidence_pack", t, "rules", None, None))
            budget = OutputBudget(min(16000 if depth == "detailed" else 20000, host.capabilities.max_output_tokens), len(pack.sections), False, "V3 full-answer budget including reasoning reserve")
            messages = writer_messages(request.original_query, blueprint, pack)
            prompt_tokens = sum(host.estimator.estimate(m.content) for m in messages)
            decision = TokenBudgetPolicy(host.capabilities).decide(fixed_tokens=prompt_tokens, requested_output_tokens=budget.max_output_tokens)
            if not decision.allowed:
                raise ValueError("V3 Writer 提示词、必要证据及输出预留超出模型窗口")
            remaining_seconds()
            t = time.perf_counter()
            sections, trace = host.v3_writer.write(request.original_query, blueprint, pack, budget,
                request_started=started, on_section=on_section, messages=messages)
            stages.append(_stage("teaching_draft", t, "llm", host.v3_writer.last_client, "high"))
            trace["writer_prompt_estimated_tokens"] = prompt_tokens
        except Exception as exc:
            trace["error"] = str(exc)
            if isinstance(exc, UnsupportedQuestionKind):
                trace.update(question_kind="UNSUPPORTED", question_form=exc.question_form, rationale=exc.rationale)
        trace["total_elapsed_ms"] = (time.perf_counter()-started)*1000
        trace["deadline_seconds"] = deadline_seconds
        trace["planner_attempts"] = getattr(host.v3_planner, "attempts", [])
        trace["planner_retry_count"] = max(0, len(trace["planner_attempts"])-1)
        if blueprint is None and trace["completion_status"] == "failed" and getattr(host.v3_planner, "last_response", None):
            trace["rejected_blueprint_output"] = host.v3_planner.last_response
        trace["answer_depth"] = depth
        if blueprint:
            trace["question_kind"] = blueprint.question_kind
            trace["blueprint"] = blueprint.to_dict()
        if pack:
            trace["evidence_pack"] = pack.to_dict()
        if budget:
            trace["output_budget"] = budget.to_dict()
        citations = list(dict.fromkeys(c for s in sections for c in s.citations))
        text = "\n\n".join(s.markdown for s in sections)
        if not text:
            text = "V3 未生成回答：" + trace.get("error", "生成失败")
        return TeachingAnswerResult(AnswerResult(text, citations, zero_valid_citation=not citations), blueprint,
            pack.bundle if pack else package.context_bundle, None, tuple(stages), {"path": "v3", "trace": trace},
            budget=budget, completion_status=trace["completion_status"], error=trace.get("error"), failed_section=trace.get("failed_section"))
