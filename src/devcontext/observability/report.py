from __future__ import annotations

import math
from typing import Any, Sequence

from devcontext.observability.trace import (
    LLMCallTrace,
    PerfRecorder,
    SectionExecutionTrace,
)


# The only stages that partition a request's wall clock, and the only ones that
# may be summed into "attributed time". Every record in the recorder is a child
# of exactly one of these - the section traces live inside ``teaching_draft``,
# the LLM calls inside whichever stage issued them - so adding a child to its
# parent would count the same seconds twice and make ``unattributed`` meaningless.
TOP_LEVEL_STAGES: tuple[str, ...] = (
    "evidence_planning",
    "search_action_planning",
    "evidence_retrieval",
    "coverage_check",
    "explanation_planning",
    "teaching_draft",
    "teaching_review",
    "teaching_revision",
)


def _estimate_tokens(text: str) -> int:
    # Imported here rather than at module scope: this module is reachable from
    # the LLM client, and the context package is not cheap to import.
    from devcontext.context.estimator import HeuristicTokenEstimator

    return HeuristicTokenEstimator().estimate(text)


def build_perf_report(
    *,
    recorder: PerfRecorder,
    result: Any,
    query: str,
    answer_mode: str,
    depth: str | None,
    top_k: int,
    wall_clock_ms: float,
    sample_kind: str,
    timestamp: str,
) -> dict[str, Any]:
    """One request's latency baseline, in the shape the report documents."""
    trace = result.trace
    stages = [stage.to_dict() for stage in trace.stage_usage]
    top_level = [
        stage for stage in stages if stage["stage"] in TOP_LEVEL_STAGES
    ]
    attributed_ms = sum(stage["latency_ms"] for stage in top_level)
    unattributed_ms = wall_clock_ms - attributed_ms

    answer_text = result.answer_result.answer
    llm = recorder.llm_totals()
    sections = [item for item in recorder.section_executions if not item.revision]
    revisions = [item for item in recorder.section_executions if item.revision]
    llm_intervals = _interval_metrics(recorder.llm_calls)
    section_intervals = _interval_metrics(recorder.section_executions)
    raw_teaching = getattr(trace, "teaching", None)
    teaching = raw_teaching if isinstance(raw_teaching, dict) else {}
    section_budget = (
        teaching.get("section_budget")
        if isinstance(teaching.get("section_budget"), dict)
        else {}
    )
    locate = (
        teaching.get("locate")
        if isinstance(teaching.get("locate"), dict)
        else {}
    )

    return {
        "query": query,
        "answer_mode": answer_mode,
        "depth": depth,
        "top_k": top_k,
        "timestamp": timestamp,
        "sample_kind": sample_kind,
        "total_latency_ms": round(wall_clock_ms, 3),
        "attributed_ms": round(attributed_ms, 3),
        "unattributed_ms": round(unattributed_ms, 3),
        "unattributed_ratio": (
            round(unattributed_ms / wall_clock_ms, 4) if wall_clock_ms else None
        ),
        "top_level_stages_used": list(TOP_LEVEL_STAGES),
        "stop_reason": trace.stop_reason,
        "summary": {
            "llm_calls": llm["call_count"],
            "embedding_calls": len(recorder.embedding_calls),
            "retrieval_actions": len(recorder.retrieval_actions),
            "sections": len(sections),
            "revisions": len(revisions),
            "planned_section_count": _planned_sections(trace),
            "planned_answer_depth": _planned_depth(trace),
            "primary_strategy": _primary_strategy(trace),
            "planner_decision_source": _planner_source(trace),
            "planner_fallback": _planner_source(trace) == "fallback",
            "preferred_sections": section_budget.get("preferred_sections"),
            "hard_max_sections": section_budget.get("hard_max_sections"),
            "target_sections": section_budget.get("target_sections"),
            "section_budget_gap_code": section_budget.get("gap_code"),
            "section_budget_gap_reason": section_budget.get("gap_reason"),
            "unsupported_requirement_ids": section_budget.get(
                "unsupported_requirement_ids", []
            ),
            "budget_allow_multi_pass": teaching.get(
                "output_budget", {}
            ).get("allow_multi_pass")
            if isinstance(teaching.get("output_budget"), dict)
            else None,
            "failed_section_ids": teaching.get("failed_section_ids", []),
            "locate_requirement_ids": locate.get("locate_requirement_ids", []),
            "locate_evidence_labels": locate.get("locate_evidence_labels", []),
            "locate_fast_path_used": locate.get("locate_fast_path_used"),
            "locate_fast_path_skip_reason": locate.get(
                "locate_fast_path_skip_reason"
            ),
            # Actual, from the provider's usage block.
            "llm_latency_ms": round(llm["latency_ms"], 3),
            "llm_input_tokens": llm["input_tokens"],
            "llm_output_tokens": llm["output_tokens"],
            "llm_reasoning_tokens": llm["reasoning_tokens"],
            "llm_visible_output_tokens": llm["visible_output_tokens"],
            "token_detail_available_calls": llm[
                "token_detail_available_calls"
            ],
            "token_detail_unavailable_calls": (
                llm["call_count"] - llm["token_detail_available_calls"]
            ),
            "discarded_llm_latency_ms": round(llm["discarded_latency_ms"], 3),
            "llm_work_ms": round(llm["latency_ms"], 3),
            "llm_active_wall_ms": llm_intervals["active_wall_ms"],
            "llm_wall_span_ms": llm_intervals["wall_span_ms"],
            "peak_llm_concurrency": llm_intervals["peak_concurrency"],
            "parallelism_ratio": (
                round(llm["latency_ms"] / llm_intervals["active_wall_ms"], 4)
                if llm_intervals["active_wall_ms"]
                else None
            ),
            "section_work_ms": round(
                sum(item.latency_ms for item in recorder.section_executions), 3
            ),
            "section_wall_ms": section_intervals["active_wall_ms"],
            "peak_section_concurrency": section_intervals["peak_concurrency"],
            "logical_sequential_llm_depth": _logical_depth(
                recorder.llm_calls
            ),
            "scheduled_llm_waves": _scheduled_waves(recorder.llm_calls),
            # Estimated from characters. Never mixed into the fields above.
            "final_answer_chars": len(answer_text),
            "final_answer_tokens_estimated": _estimate_tokens(answer_text),
        },
        "stages": stages,
        "llm_calls": [call.to_dict() for call in recorder.llm_calls],
        "embedding_calls": [call.to_dict() for call in recorder.embedding_calls],
        "retrieval_actions": [
            action.to_dict() for action in recorder.retrieval_actions
        ],
        "section_executions": [
            section.to_dict() for section in recorder.section_executions
        ],
        "repeated_embeddings": recorder.repeated_embeddings(),
    }


def _planned_sections(trace: Any) -> int | None:
    plan = getattr(trace, "explanation_plan", None)
    if not isinstance(plan, dict):
        return None
    sections = plan.get("sections")
    return len(sections) if isinstance(sections, list) else None


def _planned_depth(trace: Any) -> str | None:
    plan = getattr(trace, "explanation_plan", None)
    return plan.get("answer_depth") if isinstance(plan, dict) else None


def _primary_strategy(trace: Any) -> str | None:
    plan = getattr(trace, "explanation_plan", None)
    return plan.get("primary_strategy") if isinstance(plan, dict) else None


def _planner_source(trace: Any) -> str | None:
    plan = getattr(trace, "explanation_plan", None)
    return plan.get("decision_source") if isinstance(plan, dict) else None


def _interval_metrics(
    records: Sequence[LLMCallTrace] | Sequence[SectionExecutionTrace],
) -> dict[str, float | int]:
    intervals = sorted(
        (
            float(item.started_offset_ms),
            float(item.ended_offset_ms),
        )
        for item in records
        if item.ended_offset_ms > item.started_offset_ms
    )
    if not intervals:
        return {"active_wall_ms": 0.0, "wall_span_ms": 0.0, "peak_concurrency": 0}
    merged: list[list[float]] = []
    events: list[tuple[float, int]] = []
    for started, ended in intervals:
        if not merged or started > merged[-1][1]:
            merged.append([started, ended])
        else:
            merged[-1][1] = max(merged[-1][1], ended)
        # End events sort before start events at the same timestamp.
        events.append((started, 1))
        events.append((ended, -1))
    active = sum(ended - started for started, ended in merged)
    concurrency = 0
    peak = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        concurrency += delta
        peak = max(peak, concurrency)
    return {
        "active_wall_ms": round(active, 3),
        "wall_span_ms": round(
            max(ended for _, ended in intervals) - intervals[0][0], 3
        ),
        "peak_concurrency": peak,
    }


def _logical_depth(calls: Sequence[LLMCallTrace]) -> int:
    serial = sum(1 for call in calls if call.parallel_group_id is None)
    groups: dict[str, int] = {}
    for call in calls:
        if call.parallel_group_id is not None:
            groups[call.parallel_group_id] = max(
                groups.get(call.parallel_group_id, 0), call.attempt_index
            )
    return serial + sum(groups.values())


def _scheduled_waves(calls: Sequence[LLMCallTrace]) -> int:
    serial = sum(1 for call in calls if call.parallel_group_id is None)
    grouped: dict[str, list[LLMCallTrace]] = {}
    for call in calls:
        if call.parallel_group_id is not None:
            grouped.setdefault(call.parallel_group_id, []).append(call)
    waves = serial
    for group_calls in grouped.values():
        metrics = _interval_metrics(group_calls)
        peak = max(1, int(metrics["peak_concurrency"]))
        first_attempts = sum(1 for call in group_calls if call.attempt_index == 1)
        retries = sum(1 for call in group_calls if call.attempt_index > 1)
        waves += math.ceil(first_attempts / peak) + retries
    return waves


def render_summary(report: dict[str, Any]) -> str:
    """The terminal view: what took the time, and where it went."""
    total_s = report["total_latency_ms"] / 1000
    lines = [
        "================ DevContext Performance ================",
        "",
        f"Query:            {report['query']}",
        f"Mode / depth:     {report['answer_mode']} / {report['depth']}",
        f"Sample:           {report['sample_kind']}",
        f"Total latency:    {total_s:.1f} s",
        "",
    ]
    summary = report["summary"]
    lines += [
        f"LLM calls:        {summary['llm_calls']}"
        f"   (output tokens: {summary['llm_output_tokens']})",
        f"Embedding calls:  {summary['embedding_calls']}",
        f"Retrieval actions:{summary['retrieval_actions']:>3}",
        f"Sections:         {summary['sections']}"
        f"   (revisions: {summary['revisions']})",
        "",
        "--------------------------------------------------------",
        f"{'Stage':<30}{'Round':>6}{'Time':>12}{'%':>8}",
        "--------------------------------------------------------",
    ]
    for stage in report["stages"]:
        share = (
            stage["latency_ms"] / report["total_latency_ms"] * 100
            if report["total_latency_ms"] else 0.0
        )
        round_value = stage.get("round_index")
        lines.append(
            f"{stage['stage']:<30}"
            f"{'' if round_value is None else round_value:>6}"
            f"{stage['latency_ms'] / 1000:>11.1f}s{share:>7.1f}%"
        )
    lines += [
        "--------------------------------------------------------",
        f"{'attributed':<30}{'':>6}{report['attributed_ms'] / 1000:>11.1f}s",
        f"{'unattributed':<30}{'':>6}{report['unattributed_ms'] / 1000:>11.1f}s"
        f"{(report['unattributed_ratio'] or 0) * 100:>7.1f}%",
        "",
    ]

    by_stage: dict[str, float] = {}
    for stage in report["stages"]:
        if stage["stage"] in TOP_LEVEL_STAGES:
            by_stage[stage["stage"]] = (
                by_stage.get(stage["stage"], 0.0) + stage["latency_ms"]
            )
    ranked = sorted(by_stage.items(), key=lambda item: item[1], reverse=True)
    if ranked:
        lines.append("Top bottlenecks:")
        for index, (name, latency) in enumerate(ranked[:5], start=1):
            share = (
                latency / report["total_latency_ms"] * 100
                if report["total_latency_ms"] else 0.0
            )
            lines.append(f"  {index}. {name:<28}{latency / 1000:>8.1f}s{share:>7.1f}%")
        lines.append("")

    if summary["discarded_llm_latency_ms"]:
        lines.append(
            f"Wasted LLM time:  {summary['discarded_llm_latency_ms'] / 1000:.1f} s "
            "(calls that produced nothing usable)"
        )
    if report["repeated_embeddings"]:
        total_repeat = sum(report["repeated_embeddings"].values())
        lines.append(
            f"Repeated embeds:  {total_repeat} calls over "
            f"{len(report['repeated_embeddings'])} queries"
        )
    lines.append("========================================================")
    return "\n".join(lines)
