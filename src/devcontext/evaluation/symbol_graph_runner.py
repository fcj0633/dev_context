from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from uuid import uuid4

from devcontext.agentic import RetrievalController
from devcontext.agentic.evidence_models import SearchAction
from devcontext.code_graph.expansion import CodeGraphExpander
from devcontext.code_graph.store import CodeGraphStore
from devcontext.config import project_root
from devcontext.evidence import SourcePolicy
from devcontext.evaluation.retrieval_workflow_runner import FrozenEvidencePlanner, FrozenSearchActionPlanner, OracleCoverageChecker
from devcontext.models import SearchExecution, SearchTimings
from devcontext.planning import EvidenceRequirement
from devcontext.request import UserRequest, AnswerOptions


def load_graph_cases(path: Path) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = set()
    for case in cases:
        if set(case) != {"id", "question", "anchors", "gold", "paths"} or case["id"] in ids:
            raise ValueError("Invalid or duplicate graph benchmark case")
        if not case["gold"] or len(case["anchors"]) > 3:
            raise ValueError("Graph cases require gold symbols and at most three anchors")
        for path_value in case["paths"]:
            if len(path_value) not in {3, 5}:
                raise ValueError("Gold paths must contain one or two physical edges")
        ids.add(case["id"])
    if not cases:
        raise ValueError("Empty graph benchmark")
    return cases


class FixedCandidatePolicy:
    """Replay identical source candidates; no embedding endpoint or LLM required."""
    def __init__(self, results):
        self.results = results

    def search_scope_with_trace(self, query, scope, top_k, *, code_strategy=None):
        values = copy.deepcopy(self.results)
        return SearchExecution(values, SearchTimings(), {"CODE": list(values)}, strategy="fixed_candidates")


def run_graph_evaluation(store: CodeGraphStore, cases: list[dict]) -> dict:
    required_keys = sorted({key for case in cases for key in case["anchors"] + case["gold"]})
    with store.session() as session:
        symbols = {row["symbol_key"]: row for row in session.symbols_by_keys(required_keys)}
        missing = set(required_keys) - symbols.keys()
        if missing:
            raise ValueError(f"Benchmark symbols absent from repository snapshot: {sorted(missing)}")
        chunks = {result.id: result for result in session.chunks_for_symbols(list(symbols.values()))}
    key_by_chunk = {row["chunk_id"]: key for key, row in symbols.items()}
    expander = CodeGraphExpander(store)
    reports = []
    for case in cases:
        base = [chunks[symbols[key]["chunk_id"]] for key in case["anchors"]]
        requirement = EvidenceRequirement("ER1", case["question"], case["question"], "CORE", "CURRENT", "CODE")
        action = SearchAction("SA1", "ER1", 0, case["question"], "CODE", "fixed graph benchmark", "rules")
        execution = FixedCandidatePolicy(base).search_scope_with_trace(case["question"], "CODE", 5)
        expansion = expander.expand(action, requirement, execution)
        baseline_keys = set(case["anchors"])
        added_keys = {key_by_chunk.get(result.id) for result in expansion.results}
        gold = set(case["gold"])
        graph_keys = baseline_keys | added_keys
        exact_keys = {key_by_chunk.get(value) for value in expansion.trace.exact_added_chunks}
        relation_keys = {key_by_chunk.get(value) for value in expansion.trace.graph_added_chunks}
        paths_found = []
        for path in expansion.trace.paths:
            steps = path["edges"]
            sequence = [steps[0]["from"]]
            for step in steps:
                sequence.extend([step["edge_type"], step["to"]])
            paths_found.append(sequence)
        paths_hit = sum(path in paths_found for path in case["paths"])
        workflow_spec = {
            "requirements": [{"id": "ER1", "target": case["question"], "success_criteria": case["question"],
                "priority": "CORE", "temporal_scope": "CURRENT", "source_requirement": "CODE", "expected_satisfied": True,
                "queries": {"round_0": case["question"], "round_1": case["question"]},
                "relevant": [{"source_type": "CODE", "path_contains": chunks[symbols[key]["chunk_id"]].file_path,
                                "symbol": chunks[symbols[key]["chunk_id"]].symbol_name,
                                "start_line": chunks[symbols[key]["chunk_id"]].start_line} for key in case["gold"]]}]
        }
        outcomes = {}
        for enabled in (False, True):
            controller = RetrievalController(
                FrozenEvidencePlanner(workflow_spec), FrozenSearchActionPlanner(workflow_spec), FixedCandidatePolicy(base),
                OracleCoverageChecker(workflow_spec), SourcePolicy(), graph_expander=expander if enabled else None)
            outcome = controller.retrieve(UserRequest(case["question"], AnswerOptions("teach")), 12)
            workspace_keys = {key_by_chunk.get(ref.chunk_id) for ref in outcome.package.evidence_workspace.for_requirement("ER1")}
            satisfied = outcome.package.requirement_coverage[0].satisfied
            outcomes["on" if enabled else "off"] = {
                "state": outcome.package.retrieval_state, "core_satisfied": satisfied,
                "false_ready": outcome.package.retrieval_state == "READY" and not gold <= workspace_keys,
                "action_count": len(outcome.package.search_history),
                "workspace_gold_hits": len(gold & workspace_keys),
            }
        reports.append({
            "id": case["id"], "baseline_recall": len(gold & baseline_keys) / len(gold),
            "graph_recall": len(gold & graph_keys) / len(gold),
            "exact_unique_gold_hits": len((gold - baseline_keys) & exact_keys),
            "graph_unique_gold_hits": len((gold - baseline_keys) & relation_keys),
            "graph_rescue": bool((gold - baseline_keys) & relation_keys),
            "added_count": len(added_keys), "noise_count": len(added_keys - gold),
            "path_hits": paths_hit, "path_count": len(case["paths"]),
            "trace": expansion.trace.to_dict(), "controller": outcomes,
        })
    latencies = sorted(row["trace"]["latency_ms"] for row in reports)
    added = sum(row["added_count"] for row in reports)
    summary = {
        "case_count": len(reports), "baseline_recall": mean(row["baseline_recall"] for row in reports),
        "graph_recall": mean(row["graph_recall"] for row in reports),
        "graph_rescue_cases": sum(row["graph_rescue"] for row in reports),
        "graph_unique_gold_hits": sum(row["graph_unique_gold_hits"] for row in reports),
        "exact_unique_gold_hits": sum(row["exact_unique_gold_hits"] for row in reports),
        "noise_rate": sum(row["noise_count"] for row in reports) / added if added else 0,
        "path_recall": sum(row["path_hits"] for row in reports) / max(1, sum(row["path_count"] for row in reports)),
        "expansion_p50_ms": latencies[(len(latencies) - 1) // 2],
        "expansion_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1],
        "core_coverage_off": mean(row["controller"]["off"]["core_satisfied"] for row in reports),
        "core_coverage_on": mean(row["controller"]["on"]["core_satisfied"] for row in reports),
        "false_ready_off": sum(row["controller"]["off"]["false_ready"] for row in reports),
        "false_ready_on": sum(row["controller"]["on"]["false_ready"] for row in reports),
        "errors": sum(bool(row["trace"]["error"]) for row in reports),
    }
    return {"schema_version": 1, "repository": store.repository,
            "evaluation_mode": "fixed_candidates_and_controller_with_oracle_coverage",
            "limitations": "Controlled static-source benchmark. Does not establish live retriever recall, LLM coverage accuracy, or Fast/Full end-to-end quality.",
            "summary": summary, "cases": reports,
            "acceptance": {"rescue": summary["graph_rescue_cases"] > 0,
                           "path_recall": summary["path_recall"] == 1,
                           "coverage": summary["core_coverage_on"] >= summary["core_coverage_off"],
                           "false_ready": summary["false_ready_on"] <= summary["false_ready_off"],
                           "noise": summary["noise_rate"] <= .2,
                           "p95_target": summary["expansion_p95_ms"] < 100,
                           "no_errors": summary["errors"] == 0}}


def run_fixture_evaluation(settings) -> dict:
    from devcontext.ingestion.java_parser_runner import JavaParserRunner
    from devcontext.storage import ChunkStore
    repository = "symbol-graph-fixture-" + uuid4().hex
    store = ChunkStore(settings.database_url)
    store.initialize()
    with TemporaryDirectory(prefix="devcontext-graph-") as folder:
        analysis = JavaParserRunner().analyze(project_root() / "benchmark" / "fixtures" / "symbol-graph",
                                              Path(folder) / "java-chunks.jsonl", repository)
        try:
            store.replace_repository_snapshot(repository, analysis.chunks,
                [[0.01] * settings.embedding_dimensions for _ in analysis.chunks], analysis.symbols, analysis.edges, "fixture-only")
            report = run_graph_evaluation(CodeGraphStore(settings.database_url, repository, settings.symbol_graph_query_timeout_seconds),
                                          load_graph_cases(project_root() / "benchmark" / "symbol-graph-v1.jsonl"))
            report["analysis_diagnostics"] = analysis.diagnostics
            report["fixture_only"] = True
            return report
        finally:
            with store._connect(vectors=False) as connection:
                connection.execute("DELETE FROM knowledge_chunk WHERE repository = %s", (repository,))


def compare_workflow_graph_ab(off: dict, on: dict) -> dict:
    modes = {}
    for mode in off["summary"].keys() & on["summary"].keys():
        base, graph = off["summary"][mode], on["summary"][mode]
        base_core, graph_core = base.get("core_requirement_coverage"), graph.get("core_requirement_coverage")
        modes[mode] = {
            "core_coverage_off": base_core, "core_coverage_on": graph_core,
            "core_coverage_delta": None if base_core is None or graph_core is None else graph_core - base_core,
            "passed": (base_core is not None and graph_core is not None and graph_core >= base_core
                       and graph.get("false_ready_count", 0) <= base.get("false_ready_count", 0)
                       and base.get("execution_error_count", 0) == graph.get("execution_error_count", 0) == 0),
        }
    traces = [action["graph_trace"] for record in on.get("records", [])
              for action in record["action_candidates"] if action.get("graph_trace")]
    latencies = sorted(trace["latency_ms"] for trace in traces)
    p95 = latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None
    return {"modes": modes, "passed": bool(modes) and all(value["passed"] for value in modes.values()),
            "graph_action_count": len(traces), "graph_added_chunks": sum(len(t["graph_added_chunks"]) for t in traces),
            "exact_added_chunks": sum(len(t["exact_added_chunks"]) for t in traces),
            "graph_error_count": sum(bool(t["error"]) for t in traces),
            "expansion_p95_ms": p95, "p95_target_passed": p95 is not None and p95 < 100}
