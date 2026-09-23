from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from statistics import mean
from typing import Any

from devcontext.config import Settings, project_root
from devcontext.models import SearchExecution, SearchResult
from devcontext.retrieval.service import RetrievalService


SCHEMA_VERSION = 2
SOURCE_TYPES = {"CODE", "DOCUMENT"}
CASE_TYPES = {"CODE", "DOC", "MIXED"}
TIMING_FIELDS = (
    "query_embedding_ms",
    "keyword_sql_ms",
    "vector_sql_ms",
    "fusion_ms",
    "total_ms",
)
TARGET_FIELDS = {
    "source_type", "path_contains", "class_name", "symbol", "signature",
    "start_line", "annotation_contains", "title_contains", "heading_path",
}


def _normalize(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _group_targets(group: dict[str, Any]) -> list[dict[str, Any]]:
    return group["any_of"] if "any_of" in group else [group]


def _group_source_type(group: dict[str, Any]) -> str:
    return _group_targets(group)[0]["source_type"]


def _matches(result: SearchResult, target: dict[str, Any]) -> bool:
    if target.get("source_type") and _normalize(result.source_type) != _normalize(target["source_type"]):
        return False
    if target.get("path_contains") and _normalize(target["path_contains"]) not in _normalize(result.file_path):
        return False
    if target.get("class_name") and _normalize(target["class_name"]) != _normalize(result.class_name):
        return False
    if target.get("symbol") and _normalize(target["symbol"]) != _normalize(result.symbol_name):
        return False
    if target.get("signature") and _normalize(target["signature"]) != _normalize(result.signature):
        return False
    if target.get("start_line") is not None and result.start_line != target["start_line"]:
        return False
    if target.get("annotation_contains"):
        if _normalize(target["annotation_contains"]) not in _normalize(" ".join(result.annotations)):
            return False
    if target.get("title_contains") and _normalize(target["title_contains"]) not in _normalize(result.title):
        return False
    if target.get("heading_path") is not None:
        expected = [_normalize(item) for item in target["heading_path"]]
        actual = [_normalize(item) for item in result.heading_path]
        if actual != expected:
            return False
    return True


def _matches_group(result: SearchResult, group: dict[str, Any]) -> bool:
    return any(_matches(result, target) for target in _group_targets(group))


def recall_at(results: Sequence[SearchResult], relevant: list[dict[str, Any]], k: int) -> float:
    if not relevant:
        return 0.0
    hits = sum(any(_matches_group(result, group) for result in results[:k]) for group in relevant)
    return hits / len(relevant)


def reciprocal_rank(results: Sequence[SearchResult], relevant: list[dict[str, Any]]) -> float:
    for rank, result in enumerate(results, start=1):
        if any(_matches_group(result, group) for group in relevant):
            return 1.0 / rank
    return 0.0


def _first_relevant_rank(
    results: Sequence[SearchResult], relevant: list[dict[str, Any]], source_type: str, k: int
) -> int | None:
    groups = [group for group in relevant if _group_source_type(group) == source_type]
    for rank, result in enumerate(results[:k], start=1):
        if any(_matches_group(result, group) for group in groups):
            return rank
    return None


def _source_hit(
    results: Sequence[SearchResult], relevant: list[dict[str, Any]], source_type: str, k: int
) -> bool:
    groups = [group for group in relevant if _group_source_type(group) == source_type]
    return bool(groups) and any(
        _matches_group(result, group) for group in groups for result in results[:k]
    )


def _logical_result_key(result: SearchResult) -> tuple[Any, ...]:
    if result.source_type == "DOCUMENT":
        return (
            result.source_type, result.file_path.casefold(),
            tuple(_normalize(item) for item in result.heading_path), _normalize(result.title),
        )
    return (
        result.source_type, result.file_path.casefold(), result.chunk_type,
        _normalize(result.class_name), _normalize(result.signature or result.symbol_name),
        result.start_line,
    )


def duplicate_result_rate(results: Sequence[SearchResult], k: int = 5) -> float:
    selected = list(results[:k])
    if not selected:
        return 0.0
    return (len(selected) - len({_logical_result_key(result) for result in selected})) / len(selected)


def _validate_target(target: Any, context: str) -> str:
    if not isinstance(target, dict) or not target:
        raise ValueError(f"{context} must be a non-empty object")
    unknown = set(target) - TARGET_FIELDS
    if unknown:
        raise ValueError(f"{context} contains unsupported fields: {sorted(unknown)}")
    source_type = target.get("source_type")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"{context}.source_type must be CODE or DOCUMENT")
    required = (
        {"path_contains", "class_name", "symbol", "start_line"}
        if source_type == "CODE" else {"path_contains", "heading_path"}
    )
    missing = [field for field in sorted(required) if target.get(field) in (None, "", [])]
    if missing:
        raise ValueError(f"{context} is missing required fields: {missing}")
    if "start_line" in target and (
        not isinstance(target["start_line"], int) or target["start_line"] < 1
    ):
        raise ValueError(f"{context}.start_line must be a positive integer")
    if "heading_path" in target and (
        not isinstance(target["heading_path"], list) or not target["heading_path"]
        or not all(isinstance(item, str) and item.strip() for item in target["heading_path"])
    ):
        raise ValueError(f"{context}.heading_path must be a non-empty string list")
    return source_type


def _validate_group(group: Any, context: str) -> str:
    if not isinstance(group, dict) or not group:
        raise ValueError(f"{context} must be a non-empty object")
    if "any_of" not in group:
        return _validate_target(group, context)
    if set(group) != {"any_of"}:
        raise ValueError(f"{context} cannot mix any_of with target fields")
    alternatives = group["any_of"]
    if not isinstance(alternatives, list) or not alternatives:
        raise ValueError(f"{context}.any_of must be a non-empty list")
    sources = {
        _validate_target(target, f"{context}.any_of[{index}]")
        for index, target in enumerate(alternatives)
    }
    if len(sources) != 1:
        raise ValueError(f"{context}.any_of alternatives must share one source_type")
    return sources.pop()


def validate_cases(cases: Any) -> list[dict[str, Any]]:
    if not isinstance(cases, list) or not cases:
        raise ValueError("Benchmark must contain at least one case")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        context = f"case[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{context} must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"{context}.id must be a non-empty string")
        if case_id in seen:
            raise ValueError(f"Duplicate benchmark id: {case_id}")
        seen.add(case_id)
        case_type = case.get("type")
        if case_type not in CASE_TYPES:
            raise ValueError(f"{case_id}.type must be CODE, DOC, or MIXED")
        if not isinstance(case.get("question"), str) or not case["question"].strip():
            raise ValueError(f"{case_id}.question must be a non-empty string")
        if not isinstance(case.get("legacy"), bool):
            raise ValueError(f"{case_id}.legacy must be a boolean")
        tags = case.get("tags")
        if not isinstance(tags, list) or not tags or not all(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            raise ValueError(f"{case_id}.tags must be a non-empty string list")
        relevant = case.get("relevant")
        if not isinstance(relevant, list) or not relevant:
            raise ValueError(f"{case_id}.relevant must be a non-empty list")
        sources = {
            _validate_group(group, f"{case_id}.relevant[{group_index}]")
            for group_index, group in enumerate(relevant)
        }
        expected_sources = {
            "CODE": {"CODE"}, "DOC": {"DOCUMENT"}, "MIXED": {"CODE", "DOCUMENT"},
        }[case_type]
        if sources != expected_sources:
            raise ValueError(
                f"{case_id} requires source types {sorted(expected_sources)}, got {sorted(sources)}"
            )
        if "same-name-method" in tags:
            for group_index, group in enumerate(relevant):
                for target_index, target in enumerate(_group_targets(group)):
                    if target["source_type"] == "CODE" and not target.get("signature"):
                        raise ValueError(
                            f"{case_id}.relevant[{group_index}] target {target_index} "
                            "requires signature for same-name-method"
                        )
    return cases


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exception:
                raise ValueError(f"Invalid benchmark JSON at line {line_number}") from exception
    return validate_cases(cases)


def _case_detail(
    case: dict[str, Any], execution: SearchExecution, *, diagnostic_k: int = 10
) -> dict[str, Any]:
    results = execution.results[:diagnostic_k]
    relevant = case["relevant"]
    unmatched_at_5 = [
        group for group in relevant
        if not any(_matches_group(result, group) for result in results[:5])
    ]
    failures: list[str] = []
    if not results:
        failures.append("empty_results")
    needs_code = any(_group_source_type(group) == "CODE" for group in relevant)
    needs_document = any(_group_source_type(group) == "DOCUMENT" for group in relevant)
    if needs_code and not _source_hit(results, relevant, "CODE", 5):
        failures.append("missing_code_source")
    if needs_document and not _source_hit(results, relevant, "DOCUMENT", 5):
        failures.append("missing_document_source")
    if unmatched_at_5 and any(
        any(_matches_group(result, group) for result in results[5:diagnostic_k])
        for group in unmatched_at_5
    ):
        failures.append("relevant_after_k")
    if unmatched_at_5 and any(
        not any(_matches_group(result, group) for result in results) for group in unmatched_at_5
    ):
        failures.append("target_not_in_top_10")
    duplicate_rate = duplicate_result_rate(results, 5)
    if unmatched_at_5 and duplicate_rate > 0:
        failures.append("duplicate_crowding")
    return {
        "id": case["id"], "type": case["type"], "tags": case["tags"],
        "legacy": case["legacy"], "question": case["question"],
        "recall_at_3": recall_at(results, relevant, 3),
        "recall_at_5": recall_at(results, relevant, 5),
        "reciprocal_rank": reciprocal_rank(results, relevant),
        "code_hit_at_3": _source_hit(results, relevant, "CODE", 3),
        "code_hit_at_5": _source_hit(results, relevant, "CODE", 5),
        "doc_hit_at_3": _source_hit(results, relevant, "DOCUMENT", 3),
        "doc_hit_at_5": _source_hit(results, relevant, "DOCUMENT", 5),
        "both_sources_hit_at_3": (
            case["type"] == "MIXED" and _source_hit(results, relevant, "CODE", 3)
            and _source_hit(results, relevant, "DOCUMENT", 3)
        ),
        "both_sources_hit_at_5": (
            case["type"] == "MIXED" and _source_hit(results, relevant, "CODE", 5)
            and _source_hit(results, relevant, "DOCUMENT", 5)
        ),
        "first_relevant_code_rank": _first_relevant_rank(results, relevant, "CODE", diagnostic_k),
        "first_relevant_doc_rank": _first_relevant_rank(results, relevant, "DOCUMENT", diagnostic_k),
        "no_results": not results,
        "duplicate_result_rate_at_5": duplicate_rate,
        "timings": execution.timings.to_dict(),
        "unmatched_targets_at_5": unmatched_at_5,
        "failure_reasons": failures,
        "top_results": [result.to_dict() for result in results],
    }


def _mean(values: Iterable[float]) -> float:
    collected = list(values)
    return mean(collected) if collected else 0.0


def _p95(values: Iterable[float]) -> float:
    collected = sorted(values)
    return collected[max(0, math.ceil(0.95 * len(collected)) - 1)] if collected else 0.0


def _summary(details: list[dict[str, Any]]) -> dict[str, Any]:
    if not details:
        return {"case_count": 0, "recall_at_3": 0.0, "recall_at_5": 0.0, "mrr": 0.0}
    return {
        "case_count": len(details),
        "recall_at_3": _mean(item["recall_at_3"] for item in details),
        "recall_at_5": _mean(item["recall_at_5"] for item in details),
        "mrr": _mean(item["reciprocal_rank"] for item in details),
    }


def _strategy_metrics(strategy: str, details: list[dict[str, Any]]) -> dict[str, Any]:
    overall = _summary(details)
    by_type = {
        case_type: _summary([item for item in details if item["type"] == case_type])
        for case_type in ("CODE", "DOC", "MIXED")
    }
    code_cases = [item for item in details if item["type"] in {"CODE", "MIXED"}]
    doc_cases = [item for item in details if item["type"] in {"DOC", "MIXED"}]
    mixed_cases = [item for item in details if item["type"] == "MIXED"]
    code_ranks = [
        item["first_relevant_code_rank"] for item in code_cases
        if item["first_relevant_code_rank"] is not None and item["first_relevant_code_rank"] <= 5
    ]
    doc_ranks = [
        item["first_relevant_doc_rank"] for item in doc_cases
        if item["first_relevant_doc_rank"] is not None and item["first_relevant_doc_rank"] <= 5
    ]
    timings = {
        field: {
            "average_ms": _mean(item["timings"][field] for item in details),
            "p95_ms": _p95(item["timings"][field] for item in details),
        }
        for field in TIMING_FIELDS
    }
    legacy = [item for item in details if item["legacy"]]
    return {
        "strategy": strategy, **overall,
        "average_latency_ms": timings["total_ms"]["average_ms"],
        "by_type": by_type, "legacy": _summary(legacy),
        "code_hit_at_3": _mean(float(item["code_hit_at_3"]) for item in code_cases),
        "code_hit_at_5": _mean(float(item["code_hit_at_5"]) for item in code_cases),
        "doc_hit_at_3": _mean(float(item["doc_hit_at_3"]) for item in doc_cases),
        "doc_hit_at_5": _mean(float(item["doc_hit_at_5"]) for item in doc_cases),
        "both_sources_hit_at_3": _mean(float(item["both_sources_hit_at_3"]) for item in mixed_cases),
        "both_sources_hit_at_5": _mean(float(item["both_sources_hit_at_5"]) for item in mixed_cases),
        "no_results_rate": _mean(float(item["no_results"]) for item in details),
        "duplicate_result_rate_at_5": _mean(item["duplicate_result_rate_at_5"] for item in details),
        "mean_first_relevant_code_rank_at_5": _mean(code_ranks),
        "mean_first_relevant_doc_rank_at_5": _mean(doc_ranks),
        "timings": timings, "cases": details,
    }


def _compact_strategy(strategy: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in strategy.items() if key != "cases"}


def _flatten_numbers(value: Any, prefix: str = "") -> dict[str, float]:
    numbers: dict[str, float] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            numbers.update(_flatten_numbers(child, child_prefix))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        numbers[prefix] = float(value)
    return numbers


def compare_baseline(
    current_strategies: list[dict[str, Any]], benchmark_hash: str, baseline: dict[str, Any]
) -> dict[str, Any]:
    if baseline.get("benchmark_sha256") != benchmark_hash:
        return {
            "status": "incompatible", "reason": "benchmark_sha256 differs",
            "baseline_sha256": baseline.get("benchmark_sha256"),
            "current_sha256": benchmark_hash, "deltas": {},
        }
    current = {item["strategy"]: _compact_strategy(item) for item in current_strategies}
    expected = baseline.get("strategies", {})
    current_numbers = _flatten_numbers(current)
    baseline_numbers = _flatten_numbers(expected)
    deltas = {
        key: {
            "baseline": baseline_numbers[key], "current": current_numbers[key],
            "delta": current_numbers[key] - baseline_numbers[key],
        }
        for key in sorted(current_numbers.keys() & baseline_numbers.keys())
    }
    return {"status": "comparable", "deltas": deltas}


def build_baseline(report: dict[str, Any], name: str = "retrieval-v1") -> dict[str, Any]:
    strategies = {item["strategy"]: _compact_strategy(item) for item in report["strategies"]}
    return {
        "schema_version": SCHEMA_VERSION, "name": name,
        "generated_at": report["generated_at"], "git_commit": report["git_commit"],
        "benchmark_sha256": report["benchmark_sha256"], "case_count": report["case_count"],
        "case_distribution": report["case_distribution"],
        "thresholds": {
            "legacy_hybrid_recall_at_5": 1.0,
            "mixed_hybrid_both_sources_hit_at_5": 0.8,
        },
        "strategies": strategies,
        "case_status": {
            item["strategy"]: {
                case["id"]: {
                    "recall_at_5": case["recall_at_5"],
                    "failure_reasons": case["failure_reasons"],
                }
                for case in item["cases"]
            }
            for item in report["strategies"]
        },
    }


def _git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project_root(), text=True,
        capture_output=True, check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _acceptance(
    cases: list[dict[str, Any]], strategies: list[dict[str, Any]],
    comparison: dict[str, Any], baseline_data: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    distribution = {
        case_type: sum(case["type"] == case_type for case in cases)
        for case_type in ("CODE", "DOC", "MIXED")
    }
    hybrid = next(item for item in strategies if item["strategy"] == "hybrid")
    gates: list[dict[str, Any]] = [
        {"name": "case_count", "passed": len(cases) == 36, "current": len(cases), "target": 36},
        {
            "name": "balanced_case_distribution",
            "passed": distribution == {"CODE": 12, "DOC": 12, "MIXED": 12},
            "current": distribution, "target": {"CODE": 12, "DOC": 12, "MIXED": 12},
        },
        {
            "name": "legacy_case_count", "passed": sum(case["legacy"] for case in cases) == 12,
            "current": sum(case["legacy"] for case in cases), "target": 12,
        },
        {
            "name": "legacy_hybrid_recall_at_5", "passed": hybrid["legacy"]["recall_at_5"] >= 1.0,
            "current": hybrid["legacy"]["recall_at_5"], "target": 1.0,
        },
        {
            "name": "mixed_hybrid_both_sources_hit_at_5",
            "passed": hybrid["both_sources_hit_at_5"] >= 0.8,
            "current": hybrid["both_sources_hit_at_5"], "target": 0.8,
        },
    ]
    if baseline_data is None or comparison["status"] != "comparable":
        gates.append({
            "name": "hybrid_recall_at_5_vs_baseline", "passed": None,
            "current": hybrid["recall_at_5"], "target": None,
            "reason": "baseline missing" if baseline_data is None else comparison.get("reason"),
        })
    else:
        baseline_hybrid = baseline_data["strategies"]["hybrid"]["recall_at_5"]
        gates.append({
            "name": "hybrid_recall_at_5_vs_baseline",
            "passed": hybrid["recall_at_5"] >= baseline_hybrid,
            "current": hybrid["recall_at_5"], "target": baseline_hybrid,
        })
    return gates


def evaluate(
    settings: Settings, benchmark: Path | None = None, baseline: Path | None = None,
) -> dict[str, Any]:
    benchmark = benchmark or project_root() / "benchmark" / "cases.jsonl"
    baseline = baseline or project_root() / "benchmark" / "baselines" / "retrieval-v1.json"
    cases = load_cases(benchmark)
    benchmark_hash = sha256(benchmark.read_bytes()).hexdigest()
    service = RetrievalService(settings)
    strategies: list[dict[str, Any]] = []
    for strategy in ("keyword", "vector", "hybrid"):
        details = [
            _case_detail(case, service.search_with_trace(strategy, case["question"], top_k=10))
            for case in cases
        ]
        strategies.append(_strategy_metrics(strategy, details))

    baseline_data: dict[str, Any] | None = None
    comparison: dict[str, Any] = {"status": "missing", "deltas": {}}
    if baseline.is_file():
        baseline_data = json.loads(baseline.read_text(encoding="utf-8"))
        comparison = compare_baseline(strategies, benchmark_hash, baseline_data)

    generated_at = datetime.now(timezone.utc).isoformat()
    distribution = {
        case_type: sum(case["type"] == case_type for case in cases)
        for case_type in ("CODE", "DOC", "MIXED")
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "generated_at": generated_at,
        "git_commit": _git_commit(), "benchmark": str(benchmark),
        "benchmark_sha256": benchmark_hash, "baseline": str(baseline),
        "case_count": len(cases), "case_distribution": distribution,
        "strategies": strategies, "baseline_comparison": comparison,
    }
    report["acceptance"] = _acceptance(cases, strategies, comparison, baseline_data)
    report["quality_passed"] = all(
        gate["passed"] is True for gate in report["acceptance"] if gate["passed"] is not None
    )
    artifacts = project_root() / "artifacts"
    artifacts.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = artifacts / f"evaluation-{timestamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["output"] = str(output)
    return report
