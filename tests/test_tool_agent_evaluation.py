from dataclasses import replace

from devcontext.agentic import RetrievalController
from devcontext.evaluation.retrieval_workflow_runner import FrozenEvidencePlanner, FrozenSearchActionPlanner, OracleCoverageChecker
from devcontext.evaluation.tool_agent_runner import run_paired_evaluation
from devcontext.evidence import SourcePolicy
from devcontext.models import SearchExecution, SearchTimings
from test_tool_agent import chunk


def test_paired_runs_share_identical_plan_and_checker_configuration():
    case = {"id": "paired", "question": "question", "tags": ["choice"], "expected_retrieval_state": "READY",
            "requirements": [{"id": "ER1", "target": "f1", "success_criteria": "f1", "priority": "CORE",
             "temporal_scope": "CURRENT", "source_requirement": "CODE", "expected_satisfied": True,
             "relevant": [{"source_type": "CODE", "symbol": "f1"}], "queries": {"round_0": "f1", "round_1": "f1"}}]}
    plans, seen, checkers = [], [], []
    def plan_factory(c):
        plan = FrozenEvidencePlanner(c).plan(c["question"])
        plans.append(plan)
        return plan
    def checker_factory(c):
        checker = OracleCoverageChecker(c)
        checkers.append(checker)
        return checker
    class Policy:
        def search_scope_with_trace(self, *a, **k):
            return SearchExecution([chunk(1)], SearchTimings(), {"CODE": [chunk(1)]})
    def factory(arm, c, planner, coverage, observer):
        seen.append((arm, planner.frozen_plan))
        return RetrievalController(planner, FrozenSearchActionPlanner(c), Policy(), coverage(), SourcePolicy(), observer)
    report = run_paired_evaluation([case], factory, checker_factory, runs=3, plan_factory=plan_factory)
    assert len(plans) == 3 and len(checkers) == 9
    for i in range(3):
        assert all(p is plans[i] for _, p in seen[i * 3:i * 3 + 3])
        assert len({r["plan_sha256"] for r in report["records"][i * 3:i * 3 + 3]}) == 1
    assert all(not r["error"] for r in report["records"])
    assert all(s["core_coverage"] == 1 for s in report["summary"].values())


def test_corpus_change_rejects_paired_comparison():
    from unittest.mock import Mock
    import pytest
    case = {"id": "changed", "question": "question", "requirements": []}
    with pytest.raises(ValueError, match="Corpus changed"):
        run_paired_evaluation([case], Mock(side_effect=RuntimeError("not used")), lambda c: None,
            runs=1, corpus_fingerprint=Mock(side_effect=["before", "after"]))
