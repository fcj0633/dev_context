import time
from dataclasses import dataclass

from devcontext.agentic.retrieval_controller import _coverage_source, _stage_usage
from devcontext.context import WorkspaceCoverageView
from devcontext.deadline import RequestDeadlineExceeded, remaining_seconds
from devcontext.llm.errors import check_fatal_request
from devcontext.tool_agent.models import MAX_CALLS_PER_STEP, MAX_STEPS, MAX_TOOL_CALLS, ToolError, ToolResult
from devcontext.tool_agent.observation import build_observation
from devcontext.tool_agent.planner import PlannerFailed, RecoverablePlanningFailure, call_fingerprint, fallback_decision
from devcontext.tool_agent.policy import DecisionPolicyValidator, DecisionPolicyViolation
from devcontext.tool_agent.state import AgentRunState
from devcontext.tool_agent.state_updates import (create_run_state, apply_tool_result, record_search_action,
    update_coverage, sync_workspace_symbols, begin_step, finish_step, record_planner_failure,
    record_policy_fallback, record_stage, use_recovery, finish_run)
from devcontext.observability import mark_last_call_wasted
from devcontext.agentic.structural_coverage import combined


@dataclass
class RuntimeOutcome:
    state: AgentRunState
    latency_ms: float

    @property
    def coverage_rounds(self):
        return tuple(self.state.history.coverage_rounds)

    @property
    def search_history(self):
        return tuple(self.state.history.search_history)

    @property
    def stages(self):
        return tuple(self.state.history.stages)

    def trace(self):
        history, control = self.state.history, self.state.control
        return {"steps": [s.to_dict() for s in history.steps],
                "tool_calls": [r.to_dict() for r in history.tool_history],
                "planner_calls": sum(s.stage == "agent_planning" for s in self.stages),
                "planner_failures": control.planner_failures,
                "planner_diagnostics": control.planner_diagnostics,
                "policy_violations": control.policy_violations, "policy_fallbacks": control.policy_fallbacks,
                "coverage_calls": sum(s.stage == "coverage_check" for s in self.stages),
                "stop_reason": control.stop_reason, "recovery_used": control.recovery_used,
                "structural_evidence": self.state.workspace.structural_metadata(),
                "total_agent_ms": self.latency_ms}


class AgentRuntime:
    def __init__(self, planner, executor, coverage_checker, source_policy):
        self.planner = planner
        self.executor = executor
        self.coverage_checker = combined(coverage_checker)
        self.policy_validator = DecisionPolicyValidator()
        self.source_policy = source_policy

    def run(self, query, plan, workspace, pool):
        started = time.perf_counter()
        state = create_run_state(query, plan, workspace, pool)
        by_id = {r.id: r for r in plan.requirements}
        for step in range(MAX_STEPS):
            step_started = time.perf_counter()
            before = begin_step(state)
            try:
                remaining_seconds()
                check_fatal_request()
                view = build_observation(state, self.executor.registry)
                plan_started = time.perf_counter()
                try:
                    context = state.planner_context()
                    try:
                        decision = self.planner.plan(view, context, step)
                    except RecoverablePlanningFailure as exc:
                        record_planner_failure(state, step, diagnostic={'error': exc.error, 'response': exc.response})
                        mark_last_call_wasted("Agent planner rejected; deterministic fallback", stage="agent_planning")
                        if state.control.planner_failures > 1:
                            raise PlannerFailed(exc.error) from exc
                        decision = fallback_decision(view, context, step)
                    try:
                        self.policy_validator.validate(decision, view, context.tool_history)
                    except DecisionPolicyViolation as exc:
                        record_planner_failure(state, step, violation={'reason': str(exc), 'actions': [c.tool_name for c in decision.calls]})
                        if state.control.planner_failures > 1:
                            raise PlannerFailed(str(exc)) from exc
                        record_policy_fallback(state)
                        decision = fallback_decision(view, context, step)
                        try:
                            self.policy_validator.validate(decision, view, context.tool_history)
                        except DecisionPolicyViolation as fallback_error:
                            record_planner_failure(state, step, violation={'reason': str(fallback_error),
                                'actions': [c.tool_name for c in decision.calls], 'source': 'fallback'}, fallback=True)
                            raise PlannerFailed(str(fallback_error)) from fallback_error
                finally:
                    record_stage(state, _stage_usage("agent_planning", plan_started,
                        "fallback" if getattr(self.planner, "last_error", None) else "llm",
                        getattr(self.planner, "last_client", None), "low", round_index=step))
            except RequestDeadlineExceeded:
                finish_run(state, "DEADLINE")
                break
            except PlannerFailed:
                finish_run(state, "PLANNER_FAILED")
                break
            if decision.cannot_progress:
                finish_run(state, "NO_USEFUL_TOOL")
                break
            if not 1 <= len(decision.calls) <= MAX_CALLS_PER_STEP:
                raise ValueError("Planner violated calls per step contract")
            calls = decision.calls[:MAX_TOOL_CALLS - len(state.history.tool_history)]
            if not calls:
                finish_run(state, "MAX_TOOL_CALLS")
                break
            results = []
            acted_started = time.perf_counter()
            seen_calls = {call_fingerprint(t.call) for t in state.history.tool_history}
            for call in calls:
                if call_fingerprint(call) in seen_calls:
                    result = ToolResult(call, "INVALID_ARGUMENT", error=ToolError("REPEATED_CALL", "Identical request already attempted"))
                else:
                    result = self.executor.execute(call, by_id.get(call.requirement_id), workspace, step, view.keys_for(call.requirement_id))
                seen_calls.add(call_fingerprint(call))
                graph_store = getattr(self.executor.registry.tools, "graph_store", None) if hasattr(self.executor.registry, "tools") else None
                result = apply_tool_result(state, result, by_id[call.requirement_id], step,
                                           self.source_policy, getattr(graph_store, "repository", "repository"))
                results.append(result)
                from devcontext.observability.diagnostics import record_progress
                record_progress(plan, workspace, state.task.coverage)
                record_search_action(state, result, step, decision.decision_source)
                if result.status == "DEADLINE":
                    break
            record_stage(state, _stage_usage("evidence_retrieval", acted_started, "tools", None, "low", round_index=step))
            # Coverage and progress are evaluated only AFTER the entire batch.
            if not any(r.status == "DEADLINE" for r in results):
                check_started = time.perf_counter()
                try:
                    remaining_seconds()
                    check_fatal_request()
                    self.coverage_checker.last_client = None
                    update_coverage(state, self.coverage_checker.check(plan.requirements, WorkspaceCoverageView(workspace, round_index=step)))
                    check_fatal_request()
                except RequestDeadlineExceeded:
                    finish_run(state, "DEADLINE")
                finally:
                    record_stage(state, _stage_usage("coverage_check", check_started, _coverage_source(state.task.coverage),
                        self.coverage_checker.last_client, "low", round_index=step))
            else:
                finish_run(state, "DEADLINE")
            sync_workspace_symbols(state, step)
            record = finish_step(state, before, calls, results, step,
                (time.perf_counter() - step_started) * 1000, self.coverage_checker.last_diagnostics)
            if state.control.stop_reason:
                break
            if all(c.satisfied for c in state.task.coverage if by_id[c.requirement_id].priority == "CORE"):
                finish_run(state, "READY")
                break
            if len(state.history.tool_history) >= MAX_TOOL_CALLS:
                finish_run(state, "MAX_TOOL_CALLS")
                break
            if not record.progress:
                can_recover = not state.control.recovery_used and step + 1 < MAX_STEPS and any(r.error and r.error.retryable for r in results)
                if can_recover:
                    use_recovery(state)
                else:
                    finish_run(state, "ALL_TOOLS_FAILED" if all(not r.completed for r in state.history.tool_history) else "NO_PROGRESS")
                    break
        finish_run(state, state.control.stop_reason or "MAX_STEPS")
        return RuntimeOutcome(state, (time.perf_counter() - started) * 1000)
