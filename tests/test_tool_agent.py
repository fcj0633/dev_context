from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import json

import pytest

from devcontext.agentic.evidence_models import RequirementCoverage, SearchAction, ToolExecutionSummary, package_state
from devcontext.agentic.retrieval_engine import FixedEvidencePlanner
from devcontext.agentic.tool_driven_retrieval_controller import ToolDrivenRetrievalController
from devcontext.context import ContextBuilder, EvidenceWorkspace
from devcontext.evidence import EvidencePool, SourcePolicy
from devcontext.models import SearchResult, SearchExecution, SearchTimings
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.planning.retrieval_need import RetrievalNeed, RelationSpec, PathSpec, PathSegment
from devcontext.request import UserRequest, AnswerOptions
from devcontext.tool_agent.models import AgentDecision, AgentMemory, ToolCall, ToolResult, ToolError, ToolObservation, SymbolObservation, RelationObservation
from devcontext.tool_agent.executor import ToolExecutor
from devcontext.tool_agent.registry import ToolRegistry
from devcontext.tool_agent.tools import RepositoryTools
from devcontext.tool_agent.runtime import AgentRuntime
from devcontext.tool_agent.observation import build_observation
from devcontext.tool_agent.planner import AgentPlanner, PlannerFailed


def chunk(i, source="CODE"):
    return SearchResult(i, source, "METHOD", f"Service{i}.java", f"void f{i}() {{}}", 1, 2, "Service", f"f{i}", f"f{i}()", None, 1.)


def requirement(i="ER1", source="CODE"):
    return EvidenceRequirement(i, "业务流程", "完整下游调用", "CORE", "CURRENT", source, (RetrievalNeed("DOCUMENT" if source == "DOCUMENT" else "CODE"),))


def call(tool="search_code", value="query", rid="ER1", identifier="TA1-1"):
    arg = "symbol_key" if tool.startswith("find_") and tool != "find_symbol" else ("name" if tool == "find_symbol" else "query")
    return ToolCall(identifier, rid, tool, {arg: value}, "fill gap")


class ScriptedPlanner:
    last_client = None
    last_error = None
    def __init__(self, steps):
        self.decisions = iter(steps)
    def plan(self, *args):
        return AgentDecision(tuple(next(self.decisions)))


class Checker:
    last_client = None
    def __init__(self, ready_at=99):
        self.calls = 0
        self.ready_at = ready_at
    def check(self, requirements, view):
        self.calls += 1
        return tuple(RequirementCoverage(r.id, ("SATISFIED" if self.calls >= self.ready_at else "PARTIAL") if view.items_for(r.id) else "MISSING",
                     tuple(x.chunk_id for x in view.items_for(r.id)), ("missing",), "fixture", "rules") for r in requirements)


def runtime(steps, handler, checker=None):
    tools = RepositoryTools(None, None)
    registry = ToolRegistry(tools)
    registry.handlers = {name: handler for name in registry.specs}
    return AgentRuntime(ScriptedPlanner(steps), ToolExecutor(registry), checker or Checker(), SourcePolicy())


def run(rt, requirements=None):
    plan = EvidencePlan("question", tuple(requirements or [requirement()]))
    workspace = EvidenceWorkspace("question")
    outcome = rt.run("question", plan, workspace, EvidencePool())
    return outcome, workspace


def test_package_state_uses_all_tools_and_preserves_fixed_compatibility():
    plan = EvidencePlan("question", (requirement(),))
    bundle = ContextBuilder().build("question", [chunk(1)])
    missing = (RequirementCoverage("ER1", "PARTIAL", (1,), ("missing",), "test", "rules"),)
    failed_search = (SearchAction("SA1", "ER1", 0, "q", "CODE", "test", error="search failed"),)
    assert package_state(plan, bundle, missing, failed_search) == "RETRIEVAL_FAILED"
    assert package_state(plan, bundle, missing, failed_search, tool_summary=ToolExecutionSummary(2, 1, 1)) == "PARTIAL"
    satisfied = (replace(missing[0], state="SATISFIED"),)
    assert package_state(plan, bundle, satisfied, failed_search, tool_summary=ToolExecutionSummary(2, 1, 1)) == "READY"
    empty = ContextBuilder().build("question", [])
    assert package_state(plan, empty, missing, tool_summary=ToolExecutionSummary(1, 0, 1)) == "RETRIEVAL_FAILED"
    assert package_state(plan, empty, missing, tool_summary=ToolExecutionSummary(1, 1, 0)) == "EMPTY"


def test_no_progress_is_decided_after_whole_step_and_coverage_once():
    def handler(c, *args):
        return ToolResult(c, "SUCCESS", (chunk(2 if c.arguments["query"] == "new" else 1),))
    checker = Checker(ready_at=2)
    rt = runtime([[call(value="first")], [call(value="repeat"), call(value="new", identifier="TA2-2")]], handler, checker)
    outcome, ws = run(rt)
    assert outcome.memory.stop_reason == "READY"
    assert len(outcome.memory.tool_history) == 3
    assert outcome.memory.steps[1].new_evidence_ids == (2,)
    assert checker.calls == 2
    assert len(ws) == 2
    assert all(not r.evidence_results for r in outcome.memory.tool_history)
    assert all(not r.evidence_results for s in outcome.memory.steps for r in s.results)


def test_repeated_evidence_stops_after_completed_batch():
    outcome, _ = run(runtime([[call(value="first")], [call(value="same")]],
        lambda c, *a: ToolResult(c, "SUCCESS", (chunk(1),))))
    assert outcome.memory.stop_reason == "NO_PROGRESS"
    assert len(outcome.memory.steps) == 2


def test_ownership_for_another_requirement_counts_as_progress():
    steps = [[call(rid="ER1")], [call(rid="ER2")], [call(value="third", rid="ER2")]]
    outcome, _ = run(runtime(steps, lambda c, *a: ToolResult(c, "SUCCESS", (chunk(1),))), [requirement(), requirement("ER2")])
    assert outcome.memory.steps[1].progress
    assert outcome.memory.steps[1].new_evidence_ids == ()
    assert outcome.memory.steps[1].new_ownerships == (("ER2", 1),)
    assert outcome.memory.stop_reason == "NO_PROGRESS"


def test_one_recovery_allows_different_tool_and_second_failure_stops():
    def handler(c, *a):
        if c.tool_name == "search_code":
            return ToolResult(c, "ERROR", error=ToolError("TOOL_TIMEOUT", "timeout", True))
        return ToolResult(c, "SUCCESS", (chunk(1),))
    outcome, _ = run(runtime([[call()], [call("find_symbol", "f1")]], handler, Checker(1)))
    assert outcome.memory.recovery_used
    assert outcome.memory.stop_reason == "READY"
    fail = lambda c, *a: ToolResult(c, "ERROR", error=ToolError("TOOL_TIMEOUT", "timeout", True))
    outcome, _ = run(runtime([[call()], [call("find_symbol", "f1")]], fail))
    assert outcome.memory.stop_reason == "ALL_TOOLS_FAILED"
    assert len(outcome.memory.steps) == 2


def test_ambiguous_candidates_cannot_be_used_as_graph_handles():
    key = "M:demo.Service#f1()"
    def handler(c, *a):
        if c.tool_name == "search_code":
            return ToolResult(c, "EMPTY")
        return ToolResult(c, "AMBIGUOUS", observation=ToolObservation(candidates=(
            SymbolObservation(key, "METHOD", 1, "S.java", "CANDIDATE"),)),
            error=ToolError("AMBIGUOUS_SYMBOL", "ambiguous", True))
    outcome, _ = run(runtime([[call("find_symbol", "f1")], [call("find_callees", key)]], handler))
    assert not outcome.memory.available_symbols
    assert len(outcome.memory.policy_violations) == 1
    assert all(r.status != "INVALID_ARGUMENT" for r in outcome.memory.tool_history)


def test_confirmed_handle_not_in_current_observation_is_rejected():
    tools = ToolExecutor(ToolRegistry(RepositoryTools(None, None)))
    result = tools.execute(call("find_callees", "M:real#f()"), requirement(), EvidenceWorkspace(), 0, frozenset())
    assert result.status == "INVALID_ARGUMENT"


@pytest.mark.parametrize("source,tool", [("DOCUMENT", "search_code"), ("CODE", "search_docs"), ("DOCUMENT", "find_symbol")])
def test_source_permissions(source, tool):
    executor = ToolExecutor(ToolRegistry(RepositoryTools(None, None)))
    r = executor.execute(call(tool), requirement(source=source), EvidenceWorkspace(), 0, frozenset())
    assert r.error.code == "INVALID_TOOL_FOR_REQUIREMENT"


def test_call_budget_is_eight_even_when_planner_requests_nine():
    steps = [[call(value=f"q{i}", identifier=f"TA{s}-{i}") for i in range(s * 3, min(s * 3 + 3, 8))] for s in range(3)]
    outcome, _ = run(runtime(steps, lambda c, *a: ToolResult(c, "SUCCESS", (chunk(int(c.arguments["query"][1:]) + 1),))))
    assert outcome.memory.stop_reason == "MAX_TOOL_CALLS"
    assert [len(s.results) for s in outcome.memory.steps] == [3, 3, 2]
    assert len(outcome.memory.tool_history) == 8


def test_planner_fallback_once_then_failure():
    class BadClient:
        def generate(self, *args):
            return "not JSON"
    planner = AgentPlanner(lambda: BadClient())
    memory = AgentMemory(coverage=(RequirementCoverage("ER1", "MISSING", (), ("gap",), "test", "rules"),))
    plan = EvidencePlan("question", (requirement(),))
    view = build_observation("question", plan, memory, ToolRegistry(RepositoryTools(None, None)))
    assert planner.plan(view, plan, memory, 0).decision_source == "fallback"
    with pytest.raises(PlannerFailed):
        planner.plan(view, plan, memory, 1)


@pytest.mark.parametrize("raw", [
    {"actions": [], "cannot_progress": False},
    {"actions": [], "cannot_progress": "true"},
    {"actions": [], "cannot_progress": True, "READY": True},
    {"actions": [None], "cannot_progress": False},
])
def test_planner_rejects_invalid_contract(raw):
    with pytest.raises(ValueError):
        AgentPlanner.parse(json.dumps(raw), 0)


def test_search_keeps_original_candidates_and_discovers_confirmed_symbols():
    execution = SearchExecution([chunk(1)], SearchTimings(), {"CODE": [chunk(1)]})
    class Policy:
        def search_scope_with_trace(self, *args, **kwargs):
            return execution
    class Store:
        @contextmanager
        def session(self):
            yield SimpleNamespace(symbols_for_chunks=lambda ids: [{"symbol_key": "M:demo#f1()", "symbol_kind": "METHOD", "chunk_id": 1, "file_path": "S.java"}])
    result = RepositoryTools(Policy(), Store()).search_code(call(), requirement(), EvidenceWorkspace(), 0)
    assert result.observation.discovered_symbols[0].state == "CONFIRMED"
    assert execution.source_candidates == {"CODE": [chunk(1)]}
    assert execution.graph_results == []


def test_controller_graph_success_after_failed_search_does_not_report_failure():
    key = "M:demo.Service#f1()"
    def handler(c, *a):
        if c.tool_name == "find_symbol":
            return ToolResult(c, "SUCCESS", (chunk(1),), ToolObservation(discovered_symbols=(SymbolObservation(key, "METHOD", 1, "S.java"),)))
        if c.tool_name == "search_code":
            return ToolResult(c, "ERROR", error=ToolError("TOOL_TIMEOUT", "timeout", True))
        return ToolResult(c, "SUCCESS", (chunk(2),))
    rt = runtime([[call("find_symbol", "f1")], [call(), call("find_callees", key)]], handler, Checker(2))
    plan = EvidencePlan("question", (requirement(),))
    result = ToolDrivenRetrievalController(FixedEvidencePlanner(plan), rt).retrieve(UserRequest("question", AnswerOptions("teach")), 5)
    assert result.package.retrieval_state == "READY"
    assert all(s.error for s in result.package.search_history)
    assert result.agent_trace["stop_reason"] == "READY"
    assert result.package.evidence_workspace._frozen


def test_same_step_cannot_depend_on_unobserved_confirmation():
    key = "M:demo#f()"
    def handler(c, *a):
        assert c.tool_name == "search_code"  # Entire invalid batch was intercepted.
        return ToolResult(c, "SUCCESS", (chunk(1),), ToolObservation(discovered_symbols=(SymbolObservation(key, "METHOD", 1, "S.java"),)))
    outcome, _ = run(runtime([[call("find_symbol", "f"), call("find_callees", key)]], handler, Checker(1)))
    assert [r.status for r in outcome.memory.tool_history] == ["SUCCESS"]
    assert len(outcome.memory.policy_violations) == outcome.memory.policy_fallbacks == 1
    assert key in outcome.memory.available_symbols


def test_deadline_preserves_evidence_from_previous_calls_in_batch():
    def handler(c, *a):
        if c.arguments["query"] == "late":
            from devcontext.deadline import RequestDeadlineExceeded
            raise RequestDeadlineExceeded("deadline")
        return ToolResult(c, "SUCCESS", (chunk(1),))
    outcome, workspace = run(runtime([[call(value="first"), call(value="late")]], handler))
    assert outcome.memory.stop_reason == "DEADLINE"
    assert len(workspace) == 1
    assert len(outcome.memory.steps[0].results) == 2


def test_analysis_contract_and_configuration_failures_propagate():
    from devcontext.code_graph.models import AnalysisContractError
    def handler(*a):
        raise AnalysisContractError("invalid analysis")
    with pytest.raises(AnalysisContractError):
        run(runtime([[call()]], handler))
    def bad_factory():
        raise ValueError("missing credentials")
    with pytest.raises(ValueError, match="credentials"):
        AgentPlanner(bad_factory).plan(None, None, AgentMemory(), 0)


def test_three_step_symbol_graph_transition_uses_only_observed_handles():
    first, second, third = "M:demo.Service#f1()", "M:demo.Api#f2()", "M:demo.Impl#f3()"
    def handler(c, *a):
        if c.tool_name == "find_symbol":
            return ToolResult(c, "SUCCESS", (chunk(1),), ToolObservation(discovered_symbols=(SymbolObservation(first, "METHOD", 1, "S.java"),)))
        if c.tool_name == "find_callees":
            return ToolResult(c, "SUCCESS", (chunk(2),), ToolObservation(
                discovered_symbols=(SymbolObservation(second, "METHOD", 2, "A.java"),),
                graph_relations=(RelationObservation(first,second,"CALLS","outgoing",1,"SYMBOL_SOLVER_EXACT",1,1),)))
        return ToolResult(c, "SUCCESS", (chunk(3),), ToolObservation(
            discovered_symbols=(SymbolObservation(third,"METHOD",3,"I.java"),),
            graph_relations=(RelationObservation(third,second,"OVERRIDES","incoming",1,"DERIVED_EXACT",1,1),)))
    r = replace(requirement(), retrieval_needs=(RetrievalNeed('PATH', path_spec=PathSpec(
        (PathSegment('CALLS'),PathSegment('OVERRIDES','INCOMING')),anchor_hint='demo.Service#f1()')),))
    outcome, ws = run(runtime([[call("find_symbol", "f1")], [call("find_callees", first)],
                              [call("find_implementations", second)]], handler, Checker(1)), [r])
    assert outcome.memory.stop_reason == "READY"
    assert len(outcome.memory.steps) == 3
    assert outcome.memory.steps[1].new_relation_count == 1
    assert len(ws.relations_for('ER1')) == 2
    assert all(r.status == "SUCCESS" for r in outcome.memory.tool_history)


@pytest.mark.parametrize("profile", ["fast", "full"])
def test_factory_agent_does_not_use_automatic_expansion(profile, monkeypatch):
    import devcontext.cli as cli
    from devcontext.config import Settings
    from devcontext.agentic.coverage import CoverageChecker
    def forbidden(*a):
        pytest.fail("Agent must not construct automatic graph expansion")
    monkeypatch.setattr(cli, "_code_graph_expander", forbidden)
    controller = cli._retrieval_controller(Settings(_env_file=None, tool_agent_enabled=True,
        symbol_graph_enabled=True, answer_engine_enabled=True, answer_profile=profile))
    assert isinstance(controller, ToolDrivenRetrievalController)
    assert type(controller.runtime.coverage_checker.semantic) is CoverageChecker
    assert controller.runtime.coverage_checker.semantic.max_attempts == 1


@pytest.mark.parametrize("profile", ["fast", "full"])
def test_default_settings_select_agent_and_allow_explicit_rollback(profile, monkeypatch):
    from devcontext.config import Settings
    import devcontext.cli as cli
    monkeypatch.delenv("TOOL_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("SYMBOL_GRAPH_ENABLED", raising=False)
    settings = Settings(_env_file=None, answer_engine_enabled=True, answer_profile=profile)
    assert settings.tool_agent_enabled is True
    assert settings.symbol_graph_enabled is False
    assert isinstance(cli._retrieval_controller(settings), ToolDrivenRetrievalController)
    monkeypatch.setenv("TOOL_AGENT_ENABLED", "false")
    rollback = Settings(_env_file=None)
    assert rollback.tool_agent_enabled is False
    assert not isinstance(cli._retrieval_controller(rollback), ToolDrivenRetrievalController)


def test_both_requirement_needs_document_and_code_before_ready():
    from devcontext.agentic.coverage import CoverageChecker
    class Client:
        def generate(self, *args):
            return json.dumps({"statuses": [{"requirement_id": "ER1", "state": "SATISFIED",
                "evidence_ids": [1, 2], "missing_criteria": [], "reason": "Both evidence sources present"}]})
    checker = CoverageChecker(lambda: Client(), max_attempts=1)
    def handler(c, *a):
        return ToolResult(c, "SUCCESS", (chunk(2, "DOCUMENT") if c.tool_name == "search_docs" else chunk(1),))
    outcome, _ = run(runtime([[call()], [call("search_docs", "design")]], handler, checker), [requirement(source="BOTH")])
    assert outcome.memory.steps[0].coverage_after[0].state == "PARTIAL"
    assert outcome.memory.stop_reason == "READY"


def test_unverified_coverage_never_announces_ready():
    from devcontext.agentic.coverage import CoverageChecker
    rt = runtime([[call()], [call(value="another")]], lambda c, *a: ToolResult(c, "SUCCESS", (chunk(1),)),
                 CoverageChecker(None, max_attempts=1))
    outcome, _ = run(rt)
    assert outcome.memory.coverage[0].state == "UNVERIFIED"
    assert outcome.memory.stop_reason == "NO_PROGRESS"
