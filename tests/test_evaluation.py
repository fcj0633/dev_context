from __future__ import annotations

from pathlib import Path
from collections import Counter

import pytest

from devcontext.evaluation.runner import (
    _case_detail,
    _policy_acceptance,
    _policy_comparison,
    _routing_metrics,
    _strategy_metrics,
    compare_baseline,
    duplicate_result_rate,
    load_cases,
    recall_at,
    reciprocal_rank,
    validate_cases,
)
from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.routing import DecisionSource, QueryType, RouteDecision


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def result(
    identifier: int,
    *,
    source_type: str = "CODE",
    path: str = "order/Service.java",
    class_name: str | None = "Service",
    symbol: str | None = "run",
    signature: str | None = "public void run()",
    start_line: int | None = 10,
    title: str | None = None,
    heading_path: list[str] | None = None,
    annotations: list[str] | None = None,
) -> SearchResult:
    return SearchResult(
        id=identifier,
        source_type=source_type,
        chunk_type="METHOD" if source_type == "CODE" else "DOCUMENT_SECTION",
        file_path=path,
        content="content",
        start_line=start_line,
        end_line=start_line,
        class_name=class_name,
        symbol_name=symbol,
        signature=signature,
        title=title,
        score=1.0,
        annotations=annotations or [],
        heading_path=heading_path or [],
    )


def code_target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "source_type": "CODE",
        "path_contains": "Service.java",
        "class_name": "Service",
        "symbol": "run",
        "start_line": 10,
    }
    target.update(overrides)
    return target


def doc_target(**overrides: object) -> dict[str, object]:
    target: dict[str, object] = {
        "source_type": "DOCUMENT",
        "path_contains": "design.md",
        "heading_path": ["系统设计", "事务处理"],
    }
    target.update(overrides)
    return target


def benchmark_case(case_type: str = "CODE") -> dict[str, object]:
    relevant: list[dict[str, object]]
    if case_type == "CODE":
        relevant = [code_target()]
    elif case_type == "DOC":
        relevant = [doc_target()]
    else:
        relevant = [code_target(), doc_target()]
    return {
        "id": f"{case_type}-001",
        "type": case_type,
        "question": "测试问题",
        "tags": ["test"],
        "legacy": False,
        "relevant": relevant,
    }


def test_matches_precise_code_and_document_fields() -> None:
    code = result(1, annotations=["@Transactional(rollbackFor = Exception.class)"])
    document = result(
        2,
        source_type="DOCUMENT",
        path="docs/design.md",
        class_name=None,
        symbol=None,
        signature=None,
        start_line=20,
        title="事务处理",
        heading_path=["系统设计", "事务处理"],
    )
    relevant = [
        code_target(
            signature=" public   void run() ",
            annotation_contains="transactional",
        ),
        doc_target(title_contains="事务"),
    ]

    assert recall_at([code, document], relevant, 2) == 1.0
    assert reciprocal_rank([document, code], relevant) == 1.0


def test_any_of_counts_as_one_requirement() -> None:
    document = result(
        1,
        source_type="DOCUMENT",
        path="docs/new-design.md",
        class_name=None,
        symbol=None,
        signature=None,
        start_line=1,
        title="事务",
        heading_path=["系统设计", "事务处理"],
    )
    relevant = [
        {
            "any_of": [
                doc_target(path_contains="old-design.md"),
                doc_target(path_contains="new-design.md"),
            ]
        }
    ]

    assert recall_at([document], relevant, 1) == 1.0


def test_validate_cases_rejects_invalid_mixed_and_same_name_target() -> None:
    mixed = benchmark_case("MIXED")
    mixed["relevant"] = [code_target()]
    with pytest.raises(ValueError, match="requires source types"):
        validate_cases([mixed])

    same_name = benchmark_case("CODE")
    same_name["tags"] = ["same-name-method"]
    with pytest.raises(ValueError, match="requires signature"):
        validate_cases([same_name])


def test_load_cases_reports_invalid_json_line(tmp_path: Path) -> None:
    benchmark = tmp_path / "cases.jsonl"
    benchmark.write_text('{"id":"ok"}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="line 2"):
        load_cases(benchmark)


def test_checked_in_benchmark_has_balanced_v2_contract() -> None:
    cases = load_cases(PROJECT_ROOT / "benchmark" / "cases.jsonl")

    assert len(cases) == 36
    assert Counter(case["type"] for case in cases) == {
        "CODE": 12,
        "DOC": 12,
        "MIXED": 12,
    }
    assert sum(case["legacy"] for case in cases) == 12


def test_document_duplicates_share_logical_heading() -> None:
    first = result(
        1,
        source_type="DOCUMENT",
        path="docs/design.md",
        class_name=None,
        symbol=None,
        signature=None,
        start_line=10,
        title="事务处理",
        heading_path=["系统设计", "事务处理"],
    )
    second = result(
        2,
        source_type="DOCUMENT",
        path="docs/design.md",
        class_name=None,
        symbol=None,
        signature=None,
        start_line=30,
        title="事务处理",
        heading_path=["系统设计", "事务处理"],
    )

    assert duplicate_result_rate([first, second], 5) == 0.5


def test_case_diagnostics_report_missing_sources_and_late_target() -> None:
    irrelevant_doc = result(
        1,
        source_type="DOCUMENT",
        path="docs/other.md",
        class_name=None,
        symbol=None,
        signature=None,
        start_line=1,
        title="其他",
        heading_path=["其他"],
    )
    relevant_code = result(6)
    case = benchmark_case("MIXED")
    execution = SearchExecution(
        results=[irrelevant_doc] * 5 + [relevant_code],
        timings=SearchTimings(total_ms=12.0),
    )

    detail = _case_detail(case, execution)

    assert detail["first_relevant_code_rank"] == 6
    assert "missing_code_source" in detail["failure_reasons"]
    assert "missing_document_source" in detail["failure_reasons"]
    assert "relevant_after_k" in detail["failure_reasons"]
    assert "target_not_in_top_10" in detail["failure_reasons"]
    assert "duplicate_crowding" in detail["failure_reasons"]


def test_case_diagnostics_report_empty_results() -> None:
    detail = _case_detail(
        benchmark_case("MIXED"),
        SearchExecution([], SearchTimings(total_ms=1.0)),
    )

    assert detail["failure_reasons"] == [
        "empty_results",
        "missing_code_source",
        "missing_document_source",
        "target_not_in_top_10",
    ]


def test_strategy_metrics_split_categories_sources_and_timings() -> None:
    cases = [benchmark_case("CODE"), benchmark_case("DOC"), benchmark_case("MIXED")]
    results = [
        [result(1)],
        [result(2, source_type="DOCUMENT", path="design.md", class_name=None,
                symbol=None, signature=None, title="事务处理",
                heading_path=["系统设计", "事务处理"])],
        [
            result(3),
            result(4, source_type="DOCUMENT", path="design.md", class_name=None,
                   symbol=None, signature=None, title="事务处理",
                   heading_path=["系统设计", "事务处理"]),
        ],
    ]
    details = [
        _case_detail(case, SearchExecution(found, SearchTimings(total_ms=10 + index)))
        for index, (case, found) in enumerate(zip(cases, results, strict=True))
    ]

    metrics = _strategy_metrics("hybrid", details)

    assert metrics["by_type"]["CODE"]["recall_at_5"] == 1.0
    assert metrics["by_type"]["DOC"]["recall_at_5"] == 1.0
    assert metrics["both_sources_hit_at_5"] == 1.0
    assert metrics["timings"]["total_ms"]["average_ms"] == 11.0
    assert metrics["timings"]["total_ms"]["p95_ms"] == 12.0


def test_baseline_comparison_requires_matching_dataset_hash() -> None:
    current = [{"strategy": "hybrid", "recall_at_5": 0.8, "cases": []}]
    baseline = {
        "benchmark_sha256": "same",
        "strategies": {"hybrid": {"strategy": "hybrid", "recall_at_5": 0.75}},
    }

    comparable = compare_baseline(current, "same", baseline)
    incompatible = compare_baseline(current, "different", baseline)

    assert comparable["status"] == "comparable"
    assert comparable["deltas"]["hybrid.recall_at_5"]["delta"] == pytest.approx(0.05)
    assert incompatible["status"] == "incompatible"


def test_routing_metrics_report_accuracy_sources_and_confusion() -> None:
    cases = [benchmark_case("CODE"), benchmark_case("DOC"), benchmark_case("MIXED")]
    decisions = [
        RouteDecision(QueryType.CODE, DecisionSource.RULES, "code"),
        RouteDecision(QueryType.MIXED, DecisionSource.LLM, "mixed"),
        RouteDecision(QueryType.MIXED, DecisionSource.FALLBACK, "fallback"),
    ]

    metrics = _routing_metrics(cases, decisions)

    assert metrics["accuracy"] == pytest.approx(2 / 3)
    assert metrics["by_type"] == {"CODE": 1.0, "DOC": 0.0, "MIXED": 1.0}
    assert metrics["decision_sources"] == {"rules": 1, "llm": 1, "fallback": 1}
    assert metrics["confusion_matrix"]["DOC"]["MIXED"] == 1
    assert metrics["cases"][1]["decision_source"] == "llm"


def test_policy_comparison_and_acceptance_use_same_run_hybrid_metrics() -> None:
    def strategy(
        name: str, code: float, doc: float, mixed: float, both: float, latency: float
    ) -> dict[str, object]:
        return {
            "strategy": name,
            "by_type": {
                "CODE": {"recall_at_5": code},
                "DOC": {"recall_at_5": doc},
                "MIXED": {"recall_at_5": mixed},
            },
            "both_sources_hit_at_5": both,
            "average_latency_ms": latency,
        }

    comparison = _policy_comparison(
        [
            strategy("hybrid", 0.5, 0.4, 0.3, 0.0, 100.0),
            strategy("routed", 0.6, 0.5, 0.5, 0.2, 150.0),
        ]  # type: ignore[arg-type]
    )
    acceptance = _policy_acceptance({"accuracy": 1.0}, comparison)

    assert comparison["metrics"]["MIXED.recall_at_5"]["delta"] == pytest.approx(0.2)
    assert comparison["metrics"]["average_latency_ms"]["delta"] == 50.0
    assert all(gate["passed"] for gate in acceptance)
