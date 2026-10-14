"""Adapt the new synthesis boundary to the existing application result."""
import time
from types import SimpleNamespace

from devcontext.explanation.budget import OutputBudget
from devcontext.execution import publish_evidence
from devcontext.observability.diagnostics import mark, record_answer_input
from .inputs import AnswerInputBuilder


class ContinuousAnswerWorkflow:
    def __init__(self, synthesizer, capabilities, estimator):
        self.synthesizer, self.capabilities, self.estimator = synthesizer, capabilities, estimator
        self.runtime_options = SimpleNamespace(generation_mode="continuous")

    def run(self, request, package, *, on_section=None):
        from devcontext.explanation.workflow import TeachingAnswerResult, _stage
        started = time.perf_counter()
        reserve = min(32768 if request.policy and request.policy.profile == "full" else 16000,
                      self.capabilities.max_output_tokens)
        budget = OutputBudget(reserve, 1, False, "continuous synthesis output reserve")
        answer_input = AnswerInputBuilder(self.capabilities, self.estimator).build(request, package, budget)
        stages = [_stage("answer_input", started, "rules", None, None)]
        record_answer_input(answer_input)
        publish_evidence(answer_input.bundle)
        started = time.perf_counter()
        result = self.synthesizer.synthesize(request, answer_input, on_section=on_section,
            request_started=getattr(self, "request_started_at", None))
        stages.append(_stage("teaching_draft", started, "llm", self.synthesizer.last_client,
                             request.policy.reasoning_effort if request.policy else None))
        result.trace["total_elapsed_ms"] = (time.perf_counter()-getattr(self, "request_started_at", started))*1000
        return TeachingAnswerResult(result.answer, None, answer_input.bundle, None, tuple(stages),
            {"path": "continuous", "trace": result.trace}, budget=budget,
            completion_status=result.trace["completion_status"], error=result.trace["error"])
