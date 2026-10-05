from __future__ import annotations

from typing import Any

from devcontext.observability.trace import PerfRecorder


# The only stages that partition a request's wall clock, and the only ones that
# may be summed into "attributed time". Every record in the recorder is a child
# of exactly one of these - the section traces live inside ``teaching_draft``,
# the LLM calls inside whichever stage issued them - so adding a child to its
# parent would count the same seconds twice and make ``unattributed`` meaningless.
TOP_LEVEL_STAGES: tuple[str, ...] = (
    "request_policy",
    "evidence_planning",
    "search_action_planning",
    "evidence_retrieval",
    "coverage_check",
    "explanation_planning",
    "teaching_planning_v3",
    "teaching_evidence_pack",
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
    policy = (getattr(trace, "teaching", None) or {}).get("request_policy")
    calls = [call.to_dict() for call in recorder.llm_calls]
    if policy:
        for call in calls:
            call["requested_reasoning_effort"] = policy["reasoning_effort"] if call["stage"] in {"explanation_planning", "teaching_planning_v3", "teaching_draft"} else "low"
            if call["reasoning_effort"] is None:
                call["reasoning_omission_reason"] = "model capability does not declare requested effort support"
    sections = [item for item in recorder.section_executions if not item.revision]
    revisions = [item for item in recorder.section_executions if item.revision]

    return {
        **({"answer_text": answer_text, "answer_plan": trace.explanation_plan,
            "retrieval_plan": trace.evidence_plan} if policy else {}),
        "query": query,
        "answer_mode": answer_mode,
        "generation_mode": (getattr(trace, "teaching", None) or {}).get("generation_mode"),
        "stream": (getattr(trace, "teaching", None) or {}) if (
            getattr(trace, "teaching", None) or {}).get("generation_mode") in {"single_stream", "v3"} else None,
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
            "sections": (getattr(trace, "teaching", None) or {}).get("sections_emitted", len(sections)),
            "revisions": len(revisions),
            "planned_section_count": _planned_sections(trace),
            # Actual, from the provider's usage block.
            "llm_latency_ms": round(llm["latency_ms"], 3),
            "llm_input_tokens": llm["input_tokens"],
            "llm_output_tokens": llm["output_tokens"],
            "llm_reasoning_tokens": (
                sum(call.reasoning_tokens for call in recorder.llm_calls)
                if recorder.llm_calls and all(call.reasoning_tokens is not None for call in recorder.llm_calls)
                else None
            ),
            "discarded_llm_latency_ms": round(llm["discarded_latency_ms"], 3),
            # Estimated from characters. Never mixed into the fields above.
            "final_answer_chars": len(answer_text),
            "final_answer_tokens_estimated": _estimate_tokens(answer_text),
        },
        "stages": stages,
        "llm_calls": calls,
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
    sections = plan.get("sections", plan.get("answer_structure"))
    return len(sections) if isinstance(sections, list) else None


def render_summary(report: dict[str, Any]) -> str:
    """The terminal view: what took the time, and where it went."""
    total_s = report["total_latency_ms"] / 1000
    lines = [
        "================ DevContext Performance ================",
        "",
        f"Query:            {report['query']}",
        f"Answer mode:      {report['answer_mode']}",
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
