from __future__ import annotations

import time
from devcontext.context.budget import TokenBudgetPolicy
from devcontext.deadline import request_deadline, remaining_seconds
from devcontext.explanation.budget import OutputBudget
from devcontext.explanation.v3.errors import UnsupportedQuestionKind
from devcontext.explanation.v3.evidence_organizer import build_writer_evidence_pack, reconcile_evidence
from devcontext.explanation.v3.prompts import writer_messages
from devcontext.models import AnswerResult


def run_v3(host, request, package, *, on_section=None):
    from devcontext.explanation.workflow import TeachingAnswerResult, _stage
    started = getattr(host, "request_started_at", time.perf_counter())
    stages = []
    blueprint = pack = budget = None
    trace = {"generation_mode": "v3", "completion_status": "failed", "sections_emitted": 0, "stream_partial": False}
    sections = ()
    deadline_seconds = None if request.policy else getattr(host, "request_timeout_seconds", 180)
    with request_deadline(deadline_seconds, started=started):
        try:
            remaining_seconds()
            if host.v3_planner is None or host.v3_writer is None:
                raise ValueError("V3 requires planner and writer")
            t = time.perf_counter()
            try:
                blueprint = host.v3_planner.plan(request, package)
            finally:
                stages.append(_stage("teaching_planning_v3", t, "llm", host.v3_planner.last_client, "high"))
            t = time.perf_counter()
            try:
                blueprint = reconcile_evidence(blueprint, package)
                pack = build_writer_evidence_pack(blueprint, package,
                    required_token_budget=host.capabilities.context_window, estimator=host.estimator,
                    permissive=request.policy is not None)
            finally:
                stages.append(_stage("teaching_evidence_pack", t, "rules", None, None))
            budget = OutputBudget(min(32768, host.capabilities.max_output_tokens), len(pack.sections), False, "Full answer and reasoning budget")
            messages = writer_messages(request.original_query, blueprint, pack, universal=request.policy is not None)
            prompt_tokens = sum(host.estimator.estimate(m.content) for m in messages)
            decision = TokenBudgetPolicy(host.capabilities).decide(fixed_tokens=prompt_tokens, requested_output_tokens=budget.max_output_tokens)
            if request.policy and not decision.allowed:
                t = time.perf_counter()
                try:
                    for max_chars in (14000, 7000, 3500):
                        pack = build_writer_evidence_pack(blueprint, package, max_chars=max_chars,
                            required_token_budget=max(1, host.capabilities.context_window-budget.max_output_tokens),
                            estimator=host.estimator, permissive=True)
                        messages = writer_messages(request.original_query, blueprint, pack, universal=True)
                        prompt_tokens = sum(host.estimator.estimate(m.content) for m in messages)
                        decision = TokenBudgetPolicy(host.capabilities).decide(fixed_tokens=prompt_tokens,
                            requested_output_tokens=budget.max_output_tokens)
                        if decision.allowed:
                            break
                finally:
                    stages.append(_stage("teaching_evidence_pack", t, "compression", None, None))
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
            trace["planner_response"] = getattr(host.v3_planner, "last_response", None)
            if isinstance(exc, UnsupportedQuestionKind):
                trace.update(question_kind="UNSUPPORTED", question_form=exc.question_form, rationale=exc.rationale)
        trace["total_elapsed_ms"] = (time.perf_counter()-started)*1000
        trace["deadline_seconds"] = request.policy.hard_timeout_seconds if request.policy else deadline_seconds
        trace["planner_attempts"] = getattr(host.v3_planner, "attempts", [])
        trace["planner_retry_count"] = max(0, len(trace["planner_attempts"])-1)
        if blueprint is None and trace["completion_status"] == "failed" and getattr(host.v3_planner, "last_response", None):
            trace["rejected_blueprint_output"] = host.v3_planner.last_response
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
