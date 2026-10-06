from __future__ import annotations

import time
from devcontext.deadline import bounded_timeout, remaining_seconds, RequestDeadlineExceeded
from devcontext.explanation.planner import _payload
from devcontext.explanation.v3.errors import PlannerFailure, UnsupportedQuestionKind
from devcontext.explanation.v3.prompts import planner_prompt, compact
from devcontext.explanation.v3.validation import parse_teaching_plan
from devcontext.llm.client import LLMMessage
from devcontext.observability import llm_stage, mark_last_call_wasted
from devcontext.llm.errors import recoverable


class TeachingPlannerV3:
    def __init__(self, client_factory, capabilities, estimator):
        self.client_factory = client_factory
        self.capabilities = capabilities
        self.estimator = estimator
        self.last_client = None
        self.attempts = []
        self.last_response = None
        self.last_error = None

    def plan(self, request, package):
        payload = _payload(request, package)
        payload["reader_assumption"] = {
            "basis": "DEFAULT", "profile": "能读基础 Java，需要解释事务、并发、缓存及跨系统一致性",
            "prerequisites": [], "explicit_background_source": request.original_query,
        }
        payload["planning_limits"] = {"claims_soft_limit": 12, "steps_soft_limit": 8, "evidence_pack_chars": 28000}
        if request.policy:
            payload["request_policy"] = request.policy.to_dict()
            payload["reader_assumption"]["profile"] = request.policy.reader_assumption
            payload["reader_assumption"]["basis"] = "DEFAULT" if request.policy.reader_assumption.startswith("会基础 Java") else "USER_EXPLICIT"
        system = planner_prompt(request.original_query, universal=request.policy is not None)
        # Provider completion tokens include high-effort reasoning, not only JSON.
        reserve = min(32768, self.capabilities.max_output_tokens)
        prompt_tokens = self.estimator.estimate(system) + self.estimator.estimate(compact(payload))
        if prompt_tokens + reserve + 1024 > self.capabilities.context_window:
            raise PlannerFailure("Planner 全量输入与输出预留超过模型窗口，未截断必要证据")
        allowed = {e for e in payload["available_citations"] if any(
            (x.get("evidence_id") == e or x.get("citation", {}).get("label") == e) and (x.get("content") or "").strip()
            for x in payload["evidence"])}
        messages = [LLMMessage("system", system), LLMMessage("user", compact(payload))]
        self.attempts = []
        self.last_response = None
        self.last_error = None
        for attempt in range(2):
            started = time.perf_counter()
            response = None
            try:
                self.last_client = self.client_factory()
                if hasattr(self.last_client, "max_tokens"):
                    self.last_client.max_tokens = reserve
                if hasattr(self.last_client, "timeout_seconds"):
                    self.last_client.timeout_seconds = bounded_timeout(
                        self.last_client.timeout_seconds if request.policy else 120)
                with llm_stage("teaching_planning_v3"):
                    response = self.last_client.generate(messages)
                self.last_response = response
                if getattr(self.last_client, "last_finish_reason", None) not in {None, "stop"}:
                    raise PlannerFailure("Planner abnormal finish")
                if request.policy:
                    from devcontext.explanation.v3.universal import parse_answer_blueprint
                    result = parse_answer_blueprint(response, allowed_labels=allowed)
                else:
                    result = parse_teaching_plan(response, allowed_labels=allowed)
                self.attempts.append({"attempt": attempt+1, "elapsed_ms": (time.perf_counter()-started)*1000,
                                      "error": None, "response": response, "issues": [], "warnings": list(result.warnings),
                                      "model": getattr(self.last_client, "model", None),
                                      "reasoning_sent": getattr(self.last_client, "reasoning_effort", None),
                                      "finish_reason": getattr(self.last_client, "last_finish_reason", None)})
                return result
            except UnsupportedQuestionKind:
                raise
            except RequestDeadlineExceeded:
                raise
            except Exception as exc:
                self.last_error = exc
                partial = getattr(self.last_client, "last_partial_response", None)
                if partial:
                    self.last_response = partial
                response = response or partial
                issues = getattr(exc, "issues", [])
                self.attempts.append({"attempt": attempt+1, "elapsed_ms": (time.perf_counter()-started)*1000,
                                      "error": str(exc), "response": response, "issues": issues,
                                      "model": getattr(self.last_client, "model", None),
                                      "finish_reason": getattr(self.last_client, "last_finish_reason", None)})
                mark_last_call_wasted("V3 blueprint rejected", stage="teaching_planning_v3")
                remaining = remaining_seconds()
                if not recoverable(exc):
                    raise
                if attempt or (remaining is not None and remaining < 90):
                    raise PlannerFailure(str(exc), issues=issues) from exc
                correction = [*messages]
                if response:
                    correction.append(LLMMessage("assistant", response))
                correction.append(LLMMessage("user", "上次输出未通过检查：" + compact(issues or [{"code": "CALL_FAILED", "actual": str(exc)}]) +
                                             "。修正这些具体错误，重新输出完整合法蓝图，保持证据边界。"))
                if sum(self.estimator.estimate(m.content) for m in correction) + reserve + 1024 > self.capabilities.context_window:
                    self.attempts[-1]["retry_skipped"] = "correction exceeds model window"
                    raise PlannerFailure("Planner 修正输入超过模型窗口，进入正文兜底") from exc
                messages = correction
        raise PlannerFailure("Planner did not produce blueprint")
