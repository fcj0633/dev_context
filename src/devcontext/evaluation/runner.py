from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devcontext.config import Settings, project_root
from devcontext.models import SearchResult
from devcontext.retrieval.service import RetrievalService


@dataclass(slots=True)
class StrategyMetrics:
    strategy: str
    recall_at_3: float
    recall_at_5: float
    mrr: float
    average_latency_ms: float
    cases: list[dict[str, Any]]


def _matches(result: SearchResult, relevant: dict[str, Any]) -> bool:
    if relevant.get("source_type") and result.source_type != relevant["source_type"]:
        return False
    if relevant.get("path_contains") and relevant["path_contains"].lower() not in result.file_path.lower():
        return False
    if relevant.get("path_contains_any") and not any(
        value.lower() in result.file_path.lower()
        for value in relevant["path_contains_any"]
    ):
        return False
    if relevant.get("symbol") and relevant["symbol"].lower() != (result.symbol_name or "").lower():
        return False
    if relevant.get("title_contains") and relevant["title_contains"].lower() not in (result.title or "").lower():
        return False
    return True


def recall_at(results: list[SearchResult], relevant: list[dict[str, Any]], k: int) -> float:
    if not relevant:
        return 0.0
    hits = sum(any(_matches(result, target) for result in results[:k]) for target in relevant)
    return hits / len(relevant)


def reciprocal_rank(results: list[SearchResult], relevant: list[dict[str, Any]]) -> float:
    for rank, result in enumerate(results, start=1):
        if any(_matches(result, target) for target in relevant):
            return 1.0 / rank
    return 0.0


def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def evaluate(settings: Settings, benchmark: Path | None = None) -> dict[str, Any]:
    benchmark = benchmark or project_root() / "benchmark" / "cases.jsonl"
    cases = load_cases(benchmark)
    service = RetrievalService(settings)
    strategies: list[StrategyMetrics] = []
    for strategy in ("keyword", "vector", "hybrid"):
        details: list[dict[str, Any]] = []
        for case in cases:
            started = time.perf_counter()
            results = service.search(strategy, case["question"], top_k=10)
            latency_ms = (time.perf_counter() - started) * 1000
            details.append(
                {
                    "id": case["id"],
                    "question": case["question"],
                    "recall_at_3": recall_at(results, case["relevant"], 3),
                    "recall_at_5": recall_at(results, case["relevant"], 5),
                    "reciprocal_rank": reciprocal_rank(results, case["relevant"]),
                    "latency_ms": round(latency_ms, 2),
                    "top_results": [result.to_dict() for result in results[:5]],
                }
            )
        count = len(details) or 1
        strategies.append(
            StrategyMetrics(
                strategy=strategy,
                recall_at_3=sum(item["recall_at_3"] for item in details) / count,
                recall_at_5=sum(item["recall_at_5"] for item in details) / count,
                mrr=sum(item["reciprocal_rank"] for item in details) / count,
                average_latency_ms=sum(item["latency_ms"] for item in details) / count,
                cases=details,
            )
        )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": str(benchmark),
        "case_count": len(cases),
        "strategies": [asdict(item) for item in strategies],
    }
    artifacts = project_root() / "artifacts"
    artifacts.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = artifacts / f"evaluation-{timestamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["output"] = str(output)
    return report
