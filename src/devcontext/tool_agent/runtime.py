import time
from dataclasses import dataclass, replace

from devcontext.agentic.evidence_models import CoverageRound, RequirementCoverage, SearchAction
from devcontext.agentic.retrieval_controller import _coverage_source, _stage_usage
from devcontext.context import WorkspaceCoverageView
from devcontext.deadline import RequestDeadlineExceeded, remaining_seconds
from devcontext.llm.errors import check_fatal_request
from devcontext.tool_agent.models import ActionStep, AgentMemory, MAX_CALLS_PER_STEP, MAX_STEPS, MAX_TOOL_CALLS, ToolError, ToolResult
from devcontext.tool_agent.observation import build_observation
from devcontext.tool_agent.planner import PlannerFailed, call_fingerprint, fallback_decision
from devcontext.tool_agent.policy import DecisionPolicyValidator, DecisionPolicyViolation
from devcontext.tool_agent.structure import ingest_tool_structure
from devcontext.agentic.structural_coverage import combined


@dataclass
class RuntimeOutcome:
    memory: AgentMemory
    coverage_rounds: tuple[CoverageRound, ...]
    search_history: tuple[SearchAction, ...]
    stages: tuple
    latency_ms: float

    def trace(self):
        return {"steps": [s.to_dict() for s in self.memory.steps],
                "tool_calls": [r.to_dict() for r in self.memory.tool_history],
                "planner_calls": sum(s.stage == "agent_planning" for s in self.stages),
                "planner_failures": self.memory.planner_failures,
                "planner_diagnostics": getattr(self.memory, 'planner_diagnostics', []),
                "policy_violations": self.memory.policy_violations, "policy_fallbacks": self.memory.policy_fallbacks,
                "coverage_calls": sum(s.stage == "coverage_check" for s in self.stages),
                "stop_reason": self.memory.stop_reason, "recovery_used": self.memory.recovery_used,
                "structural_evidence": getattr(self.memory, "structural_metadata", {}),
                "total_agent_ms": self.latency_ms}


class AgentRuntime:
    def __init__(self, planner, executor, coverage_checker, source_policy):
        self.planner = planner
        self.executor = executor
        self.coverage_checker = combined(coverage_checker)
        self.policy_validator = DecisionPolicyValidator()
        self.source_policy = source_policy

    def run(self, query, plan, workspace, pool):
        if plan.schema_version != 3:
            raise ValueError("Agent runtime requires normalized v3")
        started = time.perf_counter()
        memory = AgentMemory(coverage=tuple(RequirementCoverage(r.id, "MISSING", (), (r.success_criteria,),
                              "No evidence retrieved", "rules") for r in plan.requirements))
        by_id = {r.id: r for r in plan.requirements}
        rounds, history, stages = [], [], []
        for step in range(MAX_STEPS):
            step_started = time.perf_counter()
            before = memory.coverage
            ownership_before = {(r.id, ref.chunk_id) for r in plan.requirements for ref in workspace.for_requirement(r.id)}
            symbols_before = set(memory.available_symbols)
            evidence_before = {ref.chunk_id for ref in workspace.all()}
            relation_before = set(workspace._relations)
            relation_ownership_before = set(workspace._relation_ownership)
            try:
                remaining_seconds()
                check_fatal_request()
                view = build_observation(query, plan, memory, self.executor.registry, workspace)
                plan_started = time.perf_counter()
                try:
                    decision = self.planner.plan(view, plan, memory, step)
                    try:
                        self.policy_validator.validate(decision, view, memory)
                    except DecisionPolicyViolation as exc:
                        memory.policy_violations.append({'step': step + 1, 'reason': str(exc), 'actions': [c.tool_name for c in decision.calls]})
                        memory.planner_failures += 1
                        if memory.planner_failures > 1:
                            raise PlannerFailed(str(exc)) from exc
                        memory.policy_fallbacks += 1
                        decision = fallback_decision(view, plan, memory, step)
                        try:
                            self.policy_validator.validate(decision, view, memory)
                        except DecisionPolicyViolation as fallback_error:
                            memory.policy_violations.append({'step': step + 1, 'reason': str(fallback_error),
                                'actions': [c.tool_name for c in decision.calls], 'source': 'fallback'})
                            raise PlannerFailed(str(fallback_error)) from fallback_error
                finally:
                    stages.append(_stage_usage("agent_planning", plan_started,
                        "fallback" if getattr(self.planner, "last_error", None) else "llm",
                        getattr(self.planner, "last_client", None), "low", round_index=step))
            except RequestDeadlineExceeded:
                memory.stop_reason = "DEADLINE"
                break
            except PlannerFailed:
                memory.stop_reason = "PLANNER_FAILED"
                break
            if decision.cannot_progress:
                memory.stop_reason = "NO_USEFUL_TOOL"
                break
            if not 1 <= len(decision.calls) <= MAX_CALLS_PER_STEP:
                raise ValueError("Planner violated calls per step contract")
            calls = decision.calls[:MAX_TOOL_CALLS - len(memory.tool_history)]
            if not calls:
                memory.stop_reason = "MAX_TOOL_CALLS"
                break
            results = []
            acted_started = time.perf_counter()
            seen_calls = {call_fingerprint(t.call) for t in memory.tool_history}
            for call in calls:
                if call_fingerprint(call) in seen_calls:
                    result = ToolResult(call, "INVALID_ARGUMENT", error=ToolError("REPEATED_CALL", "Identical request already attempted"))
                else:
                    result = self.executor.execute(call, by_id.get(call.requirement_id), workspace, step, view.keys_for(call.requirement_id))
                seen_calls.add(call_fingerprint(call))
                classified = [self.source_policy.classify(r, call.requirement_id) for r in result.evidence_results]
                pool.add_many(classified)
                owned_before = {r.chunk_id for r in workspace.for_requirement(call.requirement_id)}
                new_ids = workspace.ingest(classified, step)
                graph_store = getattr(self.executor.registry.tools, "graph_store", None) if hasattr(self.executor.registry, "tools") else None
                ingest_tool_structure(result, by_id[call.requirement_id], workspace, step, getattr(graph_store, "repository", "repository"))
                result = replace(result, new_evidence_count=len(new_ids),
                    new_symbol_count=len({s.symbol_key for s in result.observation.discovered_symbols
                        if s.state == "CONFIRMED" and s.symbol_key not in memory.available_symbols}),
                    new_ownership_count=len({r.id for r in result.evidence_results} - owned_before))
                results.append(result)
                memory.observe(result)
                if call.tool_name in {"search_code", "search_docs"}:
                    error = result.error.message if not result.completed and result.error else None
                    history.append(SearchAction(call.call_id, call.requirement_id, step,
                        call.arguments.get("query", ""), "CODE" if call.tool_name == "search_code" else "DOCUMENT",
                        call.reason, decision.decision_source, error))
                if result.status == "DEADLINE":
                    break
            stages.append(_stage_usage("evidence_retrieval", acted_started, "tools", None, "low", round_index=step))
            # Coverage and progress are evaluated only AFTER the entire batch.
            if not any(r.status == "DEADLINE" for r in results):
                check_started = time.perf_counter()
                try:
                    remaining_seconds()
                    check_fatal_request()
                    self.coverage_checker.last_client = None
                    memory.coverage = self.coverage_checker.check(plan.requirements, WorkspaceCoverageView(workspace, round_index=step))
                    check_fatal_request()
                except RequestDeadlineExceeded:
                    memory.stop_reason = "DEADLINE"
                finally:
                    stages.append(_stage_usage("coverage_check", check_started, _coverage_source(memory.coverage),
                        self.coverage_checker.last_client, "low", round_index=step))
            else:
                memory.stop_reason = "DEADLINE"
            from devcontext.tool_agent.models import SymbolObservation
            for requirement in plan.requirements:
                for symbol in workspace.symbols_for(requirement.id, step):
                    memory.available_symbols[symbol.symbol_key] = SymbolObservation(symbol.symbol_key, symbol.symbol_kind, symbol.chunk_id, workspace.get(symbol.chunk_id).citation.file_path)
                    memory.symbol_requirements.setdefault(symbol.symbol_key, set()).add(requirement.id)
            rounds.append(CoverageRound(step, memory.coverage))
            new_evidence = tuple(ref.chunk_id for ref in workspace.all() if ref.chunk_id not in evidence_before)
            new_symbols = tuple(k for k in memory.available_symbols if k not in symbols_before)
            ownership_after = {(r.id, ref.chunk_id) for r in plan.requirements for ref in workspace.for_requirement(r.id)}
            new_ownership = tuple(sorted(ownership_after - ownership_before))
            ranks = {"MISSING": 0, "UNVERIFIED": 0, "PARTIAL": 1, "SATISFIED": 2}
            before_states = {c.requirement_id: c.state for c in before}
            new_relations = set(workspace._relations) - relation_before
            new_relation_ownership = set(workspace._relation_ownership) - relation_ownership_before
            progress = bool(new_evidence or new_symbols or new_ownership or new_relations or new_relation_ownership or any(
                ranks[c.state] > ranks[before_states[c.requirement_id]] for c in memory.coverage))
            metadata_results = tuple(replace(r, evidence_results=(), returned_ids=tuple(e.id for e in r.evidence_results)) for r in results)
            memory.steps.append(ActionStep(step + 1, before, calls, metadata_results, new_evidence, new_symbols,
                                            new_ownership, memory.coverage, progress, (time.perf_counter() - step_started) * 1000,
                                            len(new_relations), len(new_relation_ownership), dict(self.coverage_checker.last_diagnostics)))
            if memory.stop_reason:
                break
            if all(c.satisfied for c in memory.coverage if by_id[c.requirement_id].priority == "CORE"):
                memory.stop_reason = "READY"
                break
            if len(memory.tool_history) >= MAX_TOOL_CALLS:
                memory.stop_reason = "MAX_TOOL_CALLS"
                break
            if not progress:
                can_recover = not memory.recovery_used and step + 1 < MAX_STEPS and any(r.error and r.error.retryable for r in results)
                if can_recover:
                    memory.recovery_used = True
                else:
                    memory.stop_reason = "ALL_TOOLS_FAILED" if all(not r.completed for r in memory.tool_history) else "NO_PROGRESS"
                    break
        memory.stop_reason = memory.stop_reason or "MAX_STEPS"
        memory.structural_metadata = workspace.structural_metadata()
        return RuntimeOutcome(memory, tuple(rounds), tuple(history), tuple(stages), (time.perf_counter() - started) * 1000)
