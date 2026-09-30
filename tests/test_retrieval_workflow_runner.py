from __future__ import annotations

from pathlib import Path

import pytest

from devcontext.agentic import (
    EvidencePackage,
    RequirementCoverage,
    SearchAction,
)
from devcontext.context import ContextBuilder
from devcontext.evaluation.retrieval_workflow_runner import (
    FrozenEvidencePlanner,
    FrozenSearchActionPlanner,
    OracleCoverageChecker,
    RetrievalTraceRecorder,
    build_workflow_baseline,
    compare_workflow_baseline,
    load_workflow_cases,
    run_retrieval_workflow_evaluation,
    score_workflow_case,
    validate_workflow_cases,
)
from devcontext.evidence import EvidenceAnnotation
from devcontext.models import SearchExecution, SearchResult, SearchTimings


ROOT = Path(__file__).resolve().parents[1]


def _case(*, expected: str = "READY", satisfiable: bool = True) -> dict:
    return {
        "id": "WF-001",
        "question": "入口在哪里？",
        "tags": ["locate"],
        "expected_retrieval_state": expected,
        "requirements": [
            {
                "id": "ER1",
                "target": "确认入口",
                "success_criteria": "找到入口类",
                "priority": "CORE",
                "temporal_scope": "CURRENT",
                "source_requirement": "CODE",
                "expected_satisfied": satisfiable,
                "relevant": ([{
                    "source_type": "CODE",
                    "path_contains": "Service.java",
                    "class_name": "Service",
                }] if satisfiable else []),
                "queries": {"round_0": "入口", "round_1": "Service"},
            }
        ],
    }


def _result(identifier: int = 1) -> SearchResult:
    return SearchResult(
        identifier, "CODE", "CLASS", "src/Service.java", "class Service {}",
        1, 3, "Service", None, None, None, 1.0,
    )


def _context(*results: SearchResult, truncated: bool = False):
    annotations = {
        item.id: EvidenceAnnotation("IMPLEMENTATION", "CURRENT", 100, ("ER1",))
        for item in results
    }
    bundle = ContextBuilder(max_chars=5000).build("入口在哪里？", list(results), annotations)
    if bundle.items and truncated:
        bundle.items[0].truncated = True
    return bundle


def _package(case: dict, final_context, *, state: str, second_round: bool):
    plan = FrozenEvidencePlanner(case).plan(case["question"])
    actions = [SearchAction("SA1-1", "ER1", 0, "入口", "CODE", "test")]
    if second_round:
        actions.append(SearchAction("SA2-1", "ER1", 1, "Service", "CODE", "test"))
    coverage = (
        RequirementCoverage(
            "ER1", "SATISFIED" if final_context.items else "MISSING",
            tuple(item.chunk_id for item in final_context.items),
            () if final_context.items else ("找到入口类",), "test", "rules",
        ),
    )
    return EvidencePackage(
        case["question"], plan, final_context, coverage,
        () if final_context.items else ("ER1",), state, tuple(actions),
    )


def test_checked_in_workflow_datasets_validate() -> None:
    assert len(load_workflow_cases(ROOT / "benchmark" / "l1.5-retrieval.jsonl")) == 18
    assert len(load_workflow_cases(
        ROOT / "benchmark" / "regression" / "regression-v1.jsonl",
        regression=True,
    )) == 5


def test_schema_rejects_missing_core_round_two_query() -> None:
    case = _case()
    case["requirements"][0]["queries"] = {"round_0": "入口"}

    with pytest.raises(ValueError, match="queries has invalid fields"):
        validate_workflow_cases([case])


def test_frozen_components_use_case_data_without_llm() -> None:
    case = _case()
    planner = FrozenEvidencePlanner(case)
    action_planner = FrozenSearchActionPlanner(case)
    checker = OracleCoverageChecker(case)
    plan = planner.plan(case["question"])
    actions = action_planner.plan_actions(
        case["question"], plan.requirements, round_index=0
    )
    coverage = checker.check(plan.requirements, _context(_result()))

    assert planner.last_client is None
    assert action_planner.last_client is None
    assert checker.last_client is None
    assert actions[0].query == "入口"
    assert coverage[0].state == "SATISFIED"


def test_scoring_detects_false_ready_and_context_loss() -> None:
    case = _case()
    observer = RetrievalTraceRecorder()
    observer.on_action_completed(
        SearchAction("SA1-1", "ER1", 0, "入口", "CODE", "test"),
        SearchExecution([_result()], SearchTimings()),
    )
    empty = _context()
    observer.on_context_built(0, empty)
    package = _package(case, empty, state="READY", second_round=False)

    record = score_workflow_case(case, "l1.5", "live", 1, package, observer, [])

    assert record["false_ready"] is True
    assert record["context_survival"]["candidate_found_group_count"] == 1
    assert record["context_survival"]["survived_group_count"] == 0
    assert record["context_survival"]["candidate_found_but_dropped_count"] == 1


def test_scoring_counts_second_round_rescue() -> None:
    case = _case()
    observer = RetrievalTraceRecorder()
    observer.on_context_built(0, _context())
    observer.on_action_completed(
        SearchAction("SA2-1", "ER1", 1, "Service", "CODE", "test"),
        SearchExecution([_result()], SearchTimings()),
    )
    final = _context(_result())
    observer.on_context_built(1, final)
    package = _package(case, final, state="READY", second_round=True)

    record = score_workflow_case(case, "l1.5", "frozen", 1, package, observer, [])

    assert record["full_case_success"] is True
    assert record["core_requirement_coverage"] == 1.0
    assert record["second_round_rescue"]["eligible_count"] == 1
    assert record["second_round_rescue"]["rescued_count"] == 1
    assert record["second_round_rescue"]["rescue_rate"] == 1.0


def test_unanswerable_core_is_excluded_from_rescue_denominator() -> None:
    case = _case(expected="EMPTY", satisfiable=False)
    observer = RetrievalTraceRecorder()
    empty = _context()
    observer.on_context_built(0, empty)
    observer.on_action_completed(
        SearchAction("SA2-1", "ER1", 1, "Service", "CODE", "test"),
        SearchExecution([], SearchTimings()),
    )
    observer.on_context_built(1, empty)
    package = _package(case, empty, state="EMPTY", second_round=True)

    record = score_workflow_case(case, "l1.5", "frozen", 1, package, observer, [])

    rescue = record["second_round_rescue"]
    assert rescue["eligible_count"] == 0
    assert rescue["rescue_rate"] is None
    assert rescue["no_gain_count"] == 0
    assert rescue["unanswerable_second_round_attempt_count"] == 1


def test_rescue_only_uses_actions_for_the_same_requirement() -> None:
    case = _case()
    second = {
        "id": "ER2",
        "target": "确认另一个入口",
        "success_criteria": "找到另一个入口类",
        "priority": "CORE",
        "temporal_scope": "CURRENT",
        "source_requirement": "CODE",
        "expected_satisfied": True,
        "relevant": [{
            "source_type": "CODE",
            "path_contains": "OtherService.java",
            "class_name": "OtherService",
        }],
        "queries": {"round_0": "另一个入口", "round_1": "OtherService"},
    }
    case["requirements"].append(second)
    plan = FrozenEvidencePlanner(case).plan(case["question"])
    empty = _context()
    observer = RetrievalTraceRecorder()
    observer.on_context_built(0, empty)
    actions = (
        SearchAction("SA1-1", "ER1", 0, "入口", "CODE", "test"),
        SearchAction("SA1-2", "ER2", 0, "另一个入口", "CODE", "test"),
        SearchAction("SA2-1", "ER1", 1, "Service", "CODE", "test"),
    )
    observer.on_action_completed(
        actions[-1], SearchExecution([], SearchTimings())
    )
    observer.on_context_built(1, empty)
    coverage = tuple(
        RequirementCoverage(
            requirement.id, "MISSING", (), (requirement.success_criteria,),
            "test", "rules",
        )
        for requirement in plan.requirements
    )
    package = EvidencePackage(
        case["question"], plan, empty, coverage, ("ER1", "ER2"),
        "PARTIAL", actions,
    )

    record = score_workflow_case(case, "l1.5", "frozen", 1, package, observer, [])

    rescue = record["second_round_rescue"]
    assert rescue["eligible_count"] == 1
    assert [item["requirement_id"] for item in rescue["details"]] == ["ER1"]
    assert rescue["details"][0]["round_0_queries"] == ["入口"]
    assert rescue["details"][0]["round_1_queries"] == ["Service"]


def test_rescue_distinguishes_partial_gain_from_no_gain() -> None:
    case = _case()
    case["requirements"][0]["relevant"].append({
        "source_type": "CODE",
        "path_contains": "OtherService.java",
        "class_name": "OtherService",
    })
    plan = FrozenEvidencePlanner(case).plan(case["question"])
    action = SearchAction("SA2-1", "ER1", 1, "Service", "CODE", "test")
    coverage = (
        RequirementCoverage(
            "ER1", "PARTIAL", (1,), ("找到另一个入口类",),
            "test", "rules",
        ),
    )

    partial_observer = RetrievalTraceRecorder()
    partial_observer.on_context_built(0, _context())
    partial_observer.on_action_completed(
        action, SearchExecution([_result()], SearchTimings())
    )
    partial_context = _context(_result())
    partial_observer.on_context_built(1, partial_context)
    partial_package = EvidencePackage(
        case["question"], plan, partial_context, coverage, ("ER1",),
        "PARTIAL", (action,),
    )

    partial_record = score_workflow_case(
        case, "l1.5", "frozen", 1, partial_package, partial_observer, []
    )

    assert partial_record["second_round_rescue"]["rescued_count"] == 0
    assert partial_record["second_round_rescue"]["partial_gain_count"] == 1
    assert partial_record["second_round_rescue"]["no_gain_count"] == 0

    no_gain_observer = RetrievalTraceRecorder()
    empty = _context()
    no_gain_observer.on_context_built(0, empty)
    no_gain_observer.on_action_completed(
        action, SearchExecution([], SearchTimings())
    )
    no_gain_observer.on_context_built(1, empty)
    no_gain_package = EvidencePackage(
        case["question"], plan, empty, coverage, ("ER1",),
        "PARTIAL", (action,),
    )

    no_gain_record = score_workflow_case(
        case, "l1.5", "frozen", 1, no_gain_package, no_gain_observer, []
    )

    assert no_gain_record["second_round_rescue"]["partial_gain_count"] == 0
    assert no_gain_record["second_round_rescue"]["no_gain_count"] == 1


def test_truncated_context_does_not_count_as_survived() -> None:
    case = _case()
    observer = RetrievalTraceRecorder()
    observer.on_action_completed(
        SearchAction("SA1-1", "ER1", 0, "入口", "CODE", "test"),
        SearchExecution([_result()], SearchTimings()),
    )
    final = _context(_result(), truncated=True)
    observer.on_context_built(0, final)
    package = _package(case, final, state="PARTIAL", second_round=False)

    record = score_workflow_case(case, "l1.5", "frozen", 1, package, observer, [])

    assert record["full_case_success"] is False
    assert record["context_survival"]["context_survival_rate"] == 0.0


def test_runner_records_each_regression_error_without_stopping() -> None:
    path = ROOT / "benchmark" / "regression" / "regression-v1.jsonl"
    called: list[str] = []

    def failing_factory(case, mode, observer):
        called.append(case["id"])
        raise RuntimeError("network detail must not leak")

    report = run_retrieval_workflow_evaluation(
        case_sets={"regression": (path, True)},
        modes=("frozen",), runs=1, controller_factory=failing_factory,
    )

    assert called == [f"REG-{index:03d}" for index in range(1, 6)]
    assert report["summary"]["frozen"]["execution_error_count"] == 5
    assert report["acceptance"][0]["passed"] is False
    assert all(record["error"] == "RuntimeError" for record in report["records"])


def test_workflow_baseline_requires_full_passing_frozen_run() -> None:
    report = {
        "mode": "frozen",
        "suite": "l1.5+regression",
        "generated_at": "2026-09-29T00:00:00+00:00",
        "git_commit": "abc",
        "dataset_sha256": "same",
        "source_policy_sha256": "policy",
        "run_config": {
            "only": [], "limit": None,
            "selected_case_count": 23, "dataset_case_count": 23,
        },
        "summary": {"frozen": {"full_case_success_rate": 0.5}},
        "acceptance": [{"passed": True}],
    }

    baseline = build_workflow_baseline(report)

    assert baseline["name"] == "retrieval-workflow-v1"
    assert compare_workflow_baseline(report, baseline)["status"] == "comparable"
    report["dataset_sha256"] = "different"
    assert compare_workflow_baseline(report, baseline)["status"] == "incompatible"
