"""Paired A/B/C evaluation with one immutable EvidencePlan per paired run."""
import json
import math
import time
from hashlib import sha256
from pathlib import Path
from statistics import mean

from devcontext.agentic.retrieval_engine import FixedEvidencePlanner
from devcontext.evaluation.retrieval_workflow_runner import FrozenEvidencePlanner, RetrievalTraceRecorder, validate_workflow_cases, _temporal_eligible
from devcontext.evaluation.runner import _matches_group
from devcontext.observability import PerfRecorder, capture
from devcontext.request import UserRequest, AnswerOptions
from devcontext.models import SearchResult

ARMS = ("fixed", "auto_graph", "tool_agent")


def load_tool_cases(path: Path):
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    cleaned = [{k: v for k, v in c.items() if k not in {"allowed_tool_sequences", "gold_paths", "answerable_today", "focus", "relation_gold"}} for c in cases]
    validate_workflow_cases(cleaned)
    for case in cases:
        sequences = case.get("allowed_tool_sequences", [])
        if not isinstance(sequences, list) or any(not isinstance(s, list) or not s or any(not isinstance(t, str) for t in s) for s in sequences):
            raise ValueError("Invalid allowed tool sequence")
    return cases


def plan_hash(plan):
    return sha256(json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def percentile(values, p=.95):
    return sorted(values)[max(0, math.ceil(len(values) * p) - 1)] if values else None


def _score(case, outcome, result_lookup=None):
    workspace = outcome.package.evidence_workspace
    by_id = {r.id: r for r in result_lookup([r.chunk_id for r in workspace.all()])} if result_lookup and workspace else {}
    gold_total = gold_found = core_total = core_found = 0
    all_core = True
    relation_total = relation_found = 0
    all_relation_gold = True
    for requirement in case["requirements"]:
        refs = [ref for ref in workspace.for_requirement(requirement["id"]) if _temporal_eligible(ref, requirement["temporal_scope"])] if workspace else ()
        groups = requirement["relevant"]
        # Use chunk identity/citation fields rather than answer presentation cuts.
        results = [by_id.get(ref.chunk_id) or SearchResult(ref.chunk_id, ref.citation.source_type, ref.chunk_type, ref.citation.file_path,
                    ref.content or "", ref.citation.start_line, ref.citation.end_line,
                    ref.citation.class_name, ref.citation.symbol_name, ref.citation.signature,
                    None, 0., annotations=[],
                    heading_path=list(ref.citation.heading_path)) for ref in refs]
        matched = sum(any(_matches_group(r, group) for r in results) for group in groups)
        required_relations = case.get('relation_gold', {}).get(requirement['id'], [])
        actual_relations = {(r.source, r.edge_type, r.target) for r in workspace.relations_for(requirement['id'])} if workspace is not None else set()
        relation_hits = sum(tuple(r) in actual_relations for r in required_relations)
        relation_total += len(required_relations)
        relation_found += relation_hits
        relation_complete = relation_hits == len(required_relations)
        all_relation_gold &= relation_complete
        satisfied = requirement["expected_satisfied"] and matched == len(groups) and relation_complete
        if requirement["expected_satisfied"]:
            gold_total += len(groups)
            gold_found += matched
            if requirement["priority"] == "CORE":
                core_total += 1
                core_found += satisfied
        if requirement["priority"] == "CORE":
            all_core &= bool(satisfied)
    trace = outcome.agent_trace or {}
    calls = trace.get("tool_calls", [])
    tools = [c["call"]["tool_name"] for c in calls if c["status"] not in {"INVALID_ARGUMENT", "ERROR", "DEADLINE"}]
    sequences = case.get("allowed_tool_sequences", [])
    selection = any(_subsequence(sequence, tools) for sequence in sequences) if sequences and outcome.agent_trace is not None else None
    ambiguous = []
    recovered = 0
    for c in calls:
        confirmed = {s["symbol_key"] for s in c["observation"]["discovered_symbols"] if s["state"] == "CONFIRMED"}
        for event in ambiguous:
            if not event["recovered"] and event["keys"] & confirmed:
                event["recovered"] = True
                recovered += 1
        if c["status"] == "AMBIGUOUS":
            ambiguous.append({"keys": {s["symbol_key"] for s in c["observation"]["candidates"]}, "recovered": False})
    return {"retrieval_state": outcome.package.retrieval_state,
            "core_total": core_total, "core_satisfied": core_found,
            "gold_total": gold_total, "gold_found": gold_found,
            "relation_gold_total": relation_total, "relation_gold_found": relation_found,
            "full_case_success": gold_found == gold_total and all_relation_gold and (bool(gold_total) or outcome.package.retrieval_state == case["expected_retrieval_state"]),
            "false_ready": outcome.package.retrieval_state == "READY" and not all_core,
            "tool_selection_correct": selection,
            "tool_calls": len(calls), "steps": len(trace.get("steps", [])),
            "invalid_calls": sum(c["status"] == "INVALID_ARGUMENT" for c in calls),
            "successful_calls": sum(c["status"] in {"SUCCESS", "PARTIAL"} for c in calls),
            "redundant_calls": sum((c.get("error") or {}).get("code") == "REPEATED_CALL" for c in calls),
            "graph_calls": sum(t in {"find_callers", "find_callees", "find_implementations", "find_hierarchy"} for t in tools),
            "ambiguous_events": len(ambiguous), "ambiguous_recovered": recovered,
            "no_progress_detected": trace.get("stop_reason") == "NO_PROGRESS",
            "stop_reason": trace.get("stop_reason"), "agent_trace": outcome.agent_trace,
            "package": outcome.package.to_dict(),
            "workspace": workspace.metadata_view() if workspace else [],
            "structural_evidence": workspace.structural_metadata() if workspace is not None else {},
            "policy_violations": len(trace.get("policy_violations", []))}


def _subsequence(expected, actual):
    cursor = iter(actual)
    return all(any(t == wanted for t in cursor) for wanted in expected)


def run_paired_evaluation(cases, controller_factory, coverage_factory, *, runs=3, top_k=12,
                         plan_factory=None, progress=None, corpus_fingerprint=None, checkpoint=None, result_lookup=None, checker_kind="semantic"):
    if not 1 <= runs <= 5 or not cases:
        raise ValueError("Expected cases and 1–5 runs")
    records = []
    initial_corpus = corpus_fingerprint() if corpus_fingerprint else None
    for run in range(runs):
        for case in cases:
            # This object is created ONCE, before constructing any arm. Each
            # controller receives a FixedEvidencePlanner over this SAME object.
            frozen = plan_factory(case) if plan_factory else FrozenEvidencePlanner(case).plan(case["question"])
            digest = plan_hash(frozen)
            planner = FixedEvidencePlanner(frozen)
            # Rotate order to avoid always warming caches for the same arm.
            arms = ARMS[run % 3:] + ARMS[:run % 3]
            for arm in arms:
                if progress:
                    progress(f"[{run + 1}/{runs}] {case['id']} {arm}")
                observer = RetrievalTraceRecorder()
                recorder = PerfRecorder()
                started = time.perf_counter()
                record = {"case_id": case["id"], "tags": case.get("tags", []), "run": run + 1, "arm": arm, "plan_sha256": digest,
                          "frozen_plan": frozen.to_dict(), "error": None}
                try:
                    with capture(recorder):
                        controller = controller_factory(arm, case, planner, lambda: coverage_factory(case), observer)
                        outcome = controller.retrieve(UserRequest(case["question"], AnswerOptions("teach")), top_k)
                    if outcome.package.evidence_plan is not frozen or plan_hash(frozen) != digest:
                        raise ValueError("Paired run did not retain its identical frozen EvidencePlan")
                    record.update(_score(case, outcome, result_lookup))
                    record["stage_usage"] = [s.to_dict() for s in outcome.stage_usage]
                    record["llm_calls"] = [c.to_dict() for c in recorder.llm_calls]
                    record["embedding_calls"] = [c.to_dict() for c in recorder.embedding_calls]
                    record["search_trace"] = observer.to_dict()
                except Exception as exc:
                    record["error"] = type(exc).__name__ + ": " + str(exc)[:200]
                record["latency_ms"] = (time.perf_counter() - started) * 1000
                records.append(record)
                if checkpoint:
                    checkpoint({"status": "running", "records": records})
            if corpus_fingerprint is not None and corpus_fingerprint() != initial_corpus:
                raise ValueError("Corpus changed during paired evaluation; comparisons are invalid")
    return build_paired_report(records, cases, runs=runs, top_k=top_k, corpus_sha256=initial_corpus, checker_kind=checker_kind)


def build_paired_report(records, cases, *, runs=3, top_k=12, corpus_sha256=None, checker_kind="semantic"):
    summary = {arm: summarize([r for r in records if r["arm"] == arm]) for arm in ARMS}
    a, b, c = (summary[arm] for arm in ARMS)
    graph_rescue = []
    for case in cases:
        for run in range(1, runs + 1):
            paired = {r["arm"]: r for r in records if r["case_id"] == case["id"] and r["run"] == run}
            if all(not paired[x]["error"] for x in ARMS) and paired["tool_agent"]["gold_found"] > paired["auto_graph"]["gold_found"]:
                graph_rescue.append({"case_id": case["id"], "run": run})
    complete = all(s["errors"] == 0 for s in summary.values())
    graph_b = [r for r in records if r["arm"] == "auto_graph" and not r["error"] and "graph-sensitive" in r["tags"]]
    graph_c = [r for r in records if r["arm"] == "tool_agent" and not r["error"] and "graph-sensitive" in r["tags"]]
    graph_delta = mean(r["full_case_success"] for r in graph_c) - mean(r["full_case_success"] for r in graph_b) if graph_b and graph_c else None
    gates = {"no_execution_errors": complete,
             "core_non_regression": complete and c["core_coverage"] >= b["core_coverage"],
             "false_ready_non_regression": complete and c["false_ready"] <= b["false_ready"],
             "full_case_non_regression": complete and c["full_case_success"] >= b["full_case_success"],
             "absolute_core_coverage": c["core_coverage"] >= .95 if checker_kind == 'oracle' else c["core_coverage"] > .90,
             "absolute_full_case_success": True if checker_kind == 'oracle' else c["full_case_success"] > .85,
             "absolute_false_ready": c["false_ready"] <= 5,
             "complete_acceptance_suite": len(cases) == (24 if checker_kind == 'oracle' else 16) and runs == (1 if checker_kind == 'oracle' else 3),
             "invalid_call_rate_below_5_percent": c["invalid_call_rate"] is not None and c["invalid_call_rate"] < .05,
             "mean_calls_at_most_4": c["mean_tool_calls"] <= 4,
             "p95_steps_at_most_3": c["p95_steps"] <= 3}
    stopping_cases = [r for r in records if r["arm"] == "tool_agent" and "no-progress" in r.get("tags", [])]
    if stopping_cases:
        gates["no_progress_cases_stop"] = all(not r["error"] and r["stop_reason"] in {"NO_PROGRESS", "NO_USEFUL_TOOL"} for r in stopping_cases)
    return {"schema_version": 3, "scoring_version": 3, "checker_kind": checker_kind, "status": "complete", "runs": runs, "top_k": top_k,
            "corpus_sha256": corpus_sha256,
            "planning": "same immutable frozen EvidencePlan for every arm of each paired run",
            "coverage": "same CombinedCoverage/source/structural rules and semantic factory across A/B/C",
            "arms": {"fixed": "Fixed+structural metadata", "auto_graph": "AutoGraph+structural metadata", "tool_agent": "ToolAgent+structural metadata"},
            "records": records, "summary": summary, "graph_rescue_over_auto": graph_rescue,
            "graph_sensitive_full_case_delta": graph_delta,
            "acceptance": gates, "quality_passed": all(gates.values()), "default_enabled": False,
            "limitations": "Retrieval evaluation; scripted/oracle runs do not establish live agent selection or answer quality."}


def summarize(records):
    valid = [r for r in records if not r["error"]]
    total = sum(r["core_total"] for r in valid)
    gold_total = sum(r["gold_total"] for r in valid)
    calls = sum(r["tool_calls"] for r in valid)
    selection = [r["tool_selection_correct"] for r in valid if r["tool_selection_correct"] is not None]
    return {"cases": len(records), "errors": len(records) - len(valid),
            "core_coverage": sum(r["core_satisfied"] for r in valid) / total if total else 0.,
            "evidence_recall": sum(r["gold_found"] for r in valid) / gold_total if gold_total else None,
            "full_case_success": mean(r["full_case_success"] for r in valid) if valid else 0.,
            "false_ready": sum(r["false_ready"] for r in valid),
            "mean_tool_calls": mean(r["tool_calls"] for r in valid) if valid else 0.,
            "p95_tool_calls": percentile([r["tool_calls"] for r in valid]) or 0,
            "mean_steps": mean(r["steps"] for r in valid) if valid else 0.,
            "p95_steps": percentile([r["steps"] for r in valid]) or 0,
            "invalid_call_rate": sum(r["invalid_calls"] for r in valid) / calls if calls else None,
            "tool_success_rate": sum(r["successful_calls"] for r in valid) / calls if calls else None,
            "redundant_call_rate": sum(r["redundant_calls"] for r in valid) / calls if calls else None,
            "tool_selection_accuracy": mean(selection) if selection else None,
            "graph_calls": sum(r["graph_calls"] for r in valid),
            "graph_tool_p95_ms": percentile([c["latency_ms"] for r in valid for c in (r.get("agent_trace") or {}).get("tool_calls", [])
                if c["call"]["tool_name"] in {"find_callers", "find_callees", "find_implementations", "find_hierarchy"}]),
            "ambiguous_recovery_rate": sum(r.get("ambiguous_recovered", 0) for r in valid) / sum(r.get("ambiguous_events", 0) for r in valid)
                if sum(r.get("ambiguous_events", 0) for r in valid) else None,
            "no_progress_stops": sum(r.get("no_progress_detected", False) for r in valid),
            "retrieval_p95_ms": percentile([r["latency_ms"] for r in records]),
            "planner_llm_calls": sum(c["stage"] == "agent_planning" for r in valid for c in r.get("llm_calls", [])),
            "coverage_llm_calls": sum(c["stage"] == "coverage_check" for r in valid for c in r.get("llm_calls", [])),
            "policy_violations": sum(r.get('policy_violations', 0) for r in valid),
            "relation_recall": sum(r.get('relation_gold_found', 0) for r in valid) / sum(r.get('relation_gold_total', 0) for r in valid) if sum(r.get('relation_gold_total', 0) for r in valid) else None,
            "input_tokens": _tokens(valid, "input_tokens"), "output_tokens": _tokens(valid, "output_tokens")}


def _tokens(records, field):
    values = [c.get(field) for r in records for c in r.get("llm_calls", [])]
    return sum(values) if values and all(v is not None for v in values) else None


def corpus_hash(settings):
    from devcontext.code_graph.store import CodeGraphStore
    with CodeGraphStore(settings.database_url, settings.repository_name).session() as session:
        chunks = session._execute("SELECT id, content_hash FROM knowledge_chunk WHERE repository = %s ORDER BY id",
                                  (settings.repository_name,)).fetchall()
        symbols = session._execute("SELECT symbol_key, symbol_kind, chunk_id FROM code_symbol WHERE repository = %s ORDER BY symbol_key",
                                   (settings.repository_name,)).fetchall()
        edges = session._execute("SELECT source_symbol_id, target_symbol_id, edge_type, source_line, source_column, resolution_kind FROM code_symbol_edge WHERE repository = %s ORDER BY source_symbol_id, target_symbol_id, edge_type, source_line, source_column",
                                 (settings.repository_name,)).fetchall()
    return sha256(json.dumps([chunks, symbols, edges], sort_keys=True).encode()).hexdigest()
