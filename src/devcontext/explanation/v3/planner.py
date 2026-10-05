from __future__ import annotations

import time
from devcontext.deadline import bounded_timeout, remaining_seconds, RequestDeadlineExceeded
from devcontext.explanation.planner import _payload
from devcontext.explanation.v3.errors import PlannerFailure, UnsupportedQuestionKind
from devcontext.explanation.v3.prompts import planner_prompt, compact
from devcontext.explanation.v3.validation import parse_teaching_plan
from devcontext.llm.client import LLMMessage
from devcontext.observability import llm_stage, mark_last_call_wasted


class TeachingPlannerV3:
    def __init__(self, client_factory, capabilities, estimator):
        self.client_factory = client_factory
        self.capabilities = capabilities
        self.estimator = estimator
        self.last_client = None
        self.attempts = []
        self.last_response = None

    def plan(self, request, package, *, depth):
        payload = _payload(request, package)
        payload["selected_depth"] = depth
        payload["reader_assumption"] = {
            "basis": "DEFAULT", "profile": "能读基础 Java，需要解释事务、并发、缓存及跨系统一致性",
            "prerequisites": [], "explicit_background_source": request.original_query,
        }
        payload["planning_limits"] = {"claims_soft_limit": 12, "steps_soft_limit": 8, "evidence_pack_chars": 28000}
        system = planner_prompt(request.original_query)
        # Provider completion tokens include high-effort reasoning, not only JSON.
        reserve = min(24000, self.capabilities.max_output_tokens)
        prompt_tokens = self.estimator.estimate(system) + self.estimator.estimate(compact(payload))
        if prompt_tokens + reserve + 1024 > self.capabilities.context_window:
            raise PlannerFailure("Planner 全量输入与输出预留超过模型窗口，未截断必要证据")
        allowed = {e for e in payload["available_citations"] if any(
            (x.get("evidence_id") == e or x.get("citation", {}).get("label") == e) and (x.get("content") or "").strip()
            for x in payload["evidence"])}
        messages = [LLMMessage("system", system), LLMMessage("user", compact(payload))]
        self.attempts = []
        self.last_response = None
        for attempt in range(2):
            started = time.perf_counter()
            try:
                self.last_client = self.client_factory()
                if hasattr(self.last_client, "max_tokens"):
                    self.last_client.max_tokens = (min(reserve, 12000)
                        if getattr(self.last_client, "provider", None) == "openai"
                        and getattr(self.last_client, "reasoning_effort", None) == "low" else reserve)
                if hasattr(self.last_client, "timeout_seconds"):
                    remaining = remaining_seconds()
                    # A complex blueprint can take longer than a short outline.
                    # Reserve writing time instead of failing at an arbitrary 55s.
                    ceiling = 210 if getattr(self.last_client, "provider", None) == "openai" else 120
                    planning_time = ceiling if remaining is None else min(ceiling, max(1, remaining - 55))
                    self.last_client.timeout_seconds = bounded_timeout(planning_time)
                with llm_stage("teaching_planning_v3"):
                    response = self.last_client.generate(messages)
                self.last_response = response
                if getattr(self.last_client, "last_finish_reason", None) not in {None, "stop"}:
                    raise PlannerFailure("Planner abnormal finish")
                result = parse_teaching_plan(response, allowed_labels=allowed, expected_depth=depth)
                self.attempts.append({"attempt": attempt+1, "elapsed_ms": (time.perf_counter()-started)*1000, "error": None})
                return result
            except UnsupportedQuestionKind:
                raise
            except RequestDeadlineExceeded:
                raise
            except Exception as exc:
                partial = getattr(self.last_client, "last_partial_response", None)
                if partial:
                    self.last_response = partial
                self.attempts.append({"attempt": attempt+1, "elapsed_ms": (time.perf_counter()-started)*1000, "error": str(exc)})
                mark_last_call_wasted("V3 blueprint rejected", stage="teaching_planning_v3")
                remaining = remaining_seconds()
                if attempt or (remaining is not None and remaining < 90):
                    raise PlannerFailure(str(exc)) from exc
                messages.append(LLMMessage("user", "上次输出未通过结构检查：" + str(exc) + "。请重新输出完整合法蓝图，保持证据边界。"))
        raise PlannerFailure("Planner did not produce blueprint")
