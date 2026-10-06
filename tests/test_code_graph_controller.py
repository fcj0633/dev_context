from __future__ import annotations

from types import SimpleNamespace

from devcontext.agentic import RetrievalController
from devcontext.code_graph.models import GraphExpansion, GraphExpansionTrace
from devcontext.evaluation.retrieval_workflow_runner import FrozenEvidencePlanner, FrozenSearchActionPlanner, OracleCoverageChecker, RetrievalTraceRecorder
from devcontext.evidence import SourcePolicy
from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.request import UserRequest, AnswerOptions
import devcontext.cli as cli


def result(identifier, symbol):
    return SearchResult(identifier, "CODE", "METHOD", "Service.java", f"void {symbol}() {{}}", identifier, identifier,
                        "Service", symbol, f"void {symbol}()", None, 1.0)


def test_controller_merges_graph_into_workspace_without_mutating_raw_candidates():
    base, related = result(1, "seed"), result(2, "helper")
    execution = SearchExecution([base], SearchTimings(), {"CODE": [base]}, strategy="vector")
    class Policy:
        def search_scope_with_trace(self, *args, **kwargs):
            return execution
    class Expander:
        def expand(self, action, requirement, execution, round_index):
            return GraphExpansion([related], GraphExpansionTrace(action.action_id, requirement.id, round_index,
                                                                 "CALL_CHAIN", graph_added_chunks=[2]))
    spec = {"requirements": [{"id": "ER1", "target": "调用链路", "success_criteria": "找到 helper",
            "priority": "CORE", "temporal_scope": "CURRENT", "source_requirement": "CODE", "expected_satisfied": True,
            "queries": {"round_0": "调用链路", "round_1": "seed helper"},
            "relevant": [{"source_type": "CODE", "symbol": "helper"}]}]}
    observer = RetrievalTraceRecorder()
    controller = RetrievalController(FrozenEvidencePlanner(spec), FrozenSearchActionPlanner(spec), Policy(),
        OracleCoverageChecker(spec), SourcePolicy(), observer, graph_expander=Expander())
    outcome = controller.retrieve(UserRequest("调用链路", AnswerOptions("teach")), 5)
    assert outcome.package.retrieval_state == "READY"
    refs = outcome.package.evidence_workspace.for_requirement("ER1")
    assert [ref.chunk_id for ref in refs] == [1, 2]
    assert refs[1].citation.file_path == "Service.java"
    assert refs[1].requirement_ids == ("ER1",)
    assert execution.results == [base]
    assert execution.source_candidates == {"CODE": [base]}
    assert execution.graph_results == [related]
    assert observer.action_candidates[0]["base_result_ids"] == [1]
    assert observer.action_candidates[0]["graph_trace"]["graph_added_chunks"] == [2]


def test_default_flag_does_not_construct_graph_service():
    assert cli._code_graph_expander(SimpleNamespace(symbol_graph_enabled=False)) is None


def test_followup_original_evidence_precedes_early_graph_noise_in_small_bundle():
    seed, gold, noise = result(1, "seed"), result(2, "gold"), result(3, "noise")
    class Policy:
        calls = 0
        def search_scope_with_trace(self, *args, **kwargs):
            self.calls += 1
            values = [seed] if self.calls == 1 else [gold]
            return SearchExecution(values, SearchTimings(), {"CODE": values})
    class Expander:
        def expand(self, action, requirement, execution, round_index):
            return GraphExpansion([noise], GraphExpansionTrace(action.action_id, requirement.id, round_index, "CALL_CHAIN"))
    spec = {"requirements": [{"id": "ER1", "target": "调用链路", "success_criteria": "找到 gold",
            "priority": "CORE", "temporal_scope": "CURRENT", "source_requirement": "CODE", "expected_satisfied": True,
            "queries": {"round_0": "流程", "round_1": "seed gold"},
            "relevant": [{"source_type": "CODE", "symbol": "gold"}]}]}
    controller = RetrievalController(FrozenEvidencePlanner(spec), FrozenSearchActionPlanner(spec), Policy(),
        OracleCoverageChecker(spec), SourcePolicy(), graph_expander=Expander())
    outcome = controller.retrieve(UserRequest("调用链路", AnswerOptions("teach")), 2)
    assert outcome.package.retrieval_state == "READY"
    assert [item.chunk_id for item in outcome.package.context_bundle.items] == [1, 2]
    assert {ref.chunk_id for ref in outcome.package.evidence_workspace.for_requirement("ER1")} == {1, 2, 3}


def test_cli_ab_forces_off_and_on_without_changing_budgets(monkeypatch, tmp_path):
    calls = []
    def controller(settings, **kwargs):
        calls.append(settings.symbol_graph_enabled)
    def run(**kwargs):
        kwargs["controller_factory"]({}, "live", None)
        return {"summary": {}, "acceptance": [], "quality_passed": True, "records": []}
    monkeypatch.setattr(cli, "_retrieval_controller", controller)
    monkeypatch.setattr(cli, "run_retrieval_workflow_evaluation", run)
    assert cli.main(["evaluate-retrieval-workflow", "--symbol-graph-ab", "--output", str(tmp_path / "ab.json")]) == 0
    assert calls == [False, True]


def test_ab_comparison_rejects_missing_or_decreasing_coverage():
    from devcontext.evaluation.symbol_graph_runner import compare_workflow_graph_ab
    off = {"summary": {"frozen": {"core_requirement_coverage": .9, "false_ready_count": 0, "execution_error_count": 0}}, "records": []}
    on = {"summary": {"frozen": {"core_requirement_coverage": .8, "false_ready_count": 0, "execution_error_count": 0}}, "records": []}
    assert not compare_workflow_graph_ab(off, on)["passed"]
    assert compare_workflow_graph_ab(off, off)["passed"]
    assert not compare_workflow_graph_ab({"summary": {}}, {"summary": {}})["passed"]
