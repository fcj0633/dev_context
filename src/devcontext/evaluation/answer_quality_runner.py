"""L2 answer-quality A/B: run every case through both answer modes and judge them blind.

The library primitives (`check_answer_shape`, `PairwiseAnswerJudge`) already exist in
`answer_quality.py`; this module only orchestrates them. It stays free of CLI wiring so it
can be driven from tests: callers inject both the workflow (via `workflow_factory`) and the
judge model (via `judge_client_factory`).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devcontext.agentic.models import AgenticAnswerResult
from devcontext.evaluation.answer_quality import (
    CANONICAL_BASELINE,
    CANONICAL_CANDIDATE,
    DEPTH_CHAR_RANGES,
    AnswerQualityCase,
    PairwiseAnswerJudge,
    check_answer_shape,
    load_answer_quality_cases,
)
from devcontext.evaluation.pedagogy import (
    PedagogyJudge,
    PedagogyScore,
    summarise_pedagogy,
)
from devcontext.llm import LLMClient


DEFAULT_BASELINE_MODE = "legacy"
DEFAULT_CANDIDATE_MODE = "explain"


@dataclass(frozen=True, slots=True)
class PairwiseEvaluationConfig:
    """Which two modes to compare.

    Named baseline / candidate rather than V1 / V2 so one runner covers
    legacy vs explain, explain vs teach, and teach against a later teach,
    without the judge ever needing to know a mode's name.
    """

    baseline_mode: str = DEFAULT_BASELINE_MODE
    candidate_mode: str = DEFAULT_CANDIDATE_MODE

    @property
    def modes(self) -> tuple[str, str]:
        return (self.baseline_mode, self.candidate_mode)


DEFAULT_EVALUATION_CONFIG = PairwiseEvaluationConfig()

# Kept as names for the default pair so existing imports keep working.
V1_MODE = DEFAULT_BASELINE_MODE
V2_MODE = DEFAULT_CANDIDATE_MODE
ANSWER_MODES = DEFAULT_EVALUATION_CONFIG.modes

DEFAULT_TOP_K = 12
WIN_RATE_THRESHOLD = 0.70
DETAILED_LENGTH_THRESHOLD = 0.80

# (mode, depth) -> workflow. Injected so this module never imports the CLI.
WorkflowFactory = Callable[[str, str], Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _context_digest(bundle: Any) -> list[dict[str, Any]]:
    """Enough to answer "was chunk N in the final context, and was it truncated?"."""
    return [
        {
            "label": item.citation.label,
            "chunk_id": item.chunk_id,
            "source_type": item.citation.source_type,
            "file_path": item.citation.file_path,
            "start_line": item.citation.start_line,
            "end_line": item.citation.end_line,
            "source_role": item.source_role,
            "temporal_status": item.temporal_status,
            "authority_priority": item.authority_priority,
            "truncated": item.truncated,
            "sub_question_ids": list(item.sub_question_ids),
            "requirement_ids": list(item.sub_question_ids),
        }
        for item in bundle.items
    ]


def _usage_totals(stages: Sequence[dict[str, Any]]) -> dict[str, Any]:
    llm_stages = [stage for stage in stages if stage.get("model")]
    reported = [
        stage
        for stage in llm_stages
        if stage.get("input_tokens") is not None or stage.get("output_tokens") is not None
    ]
    return {
        "llm_calls": len(llm_stages),
        "latency_ms": round(sum(float(stage["latency_ms"]) for stage in stages), 1),
        "input_tokens": sum(int(stage.get("input_tokens") or 0) for stage in llm_stages),
        "output_tokens": sum(int(stage.get("output_tokens") or 0) for stage in llm_stages),
        "stages_reporting_usage": len(reported),
    }


def _capture_arm(
    case: AnswerQualityCase, mode: str, result: AgenticAnswerResult
) -> dict[str, Any]:
    answer = result.answer_result
    trace = result.trace
    checks = check_answer_shape(case, answer.answer, list(answer.used_citations))
    stages = [usage.to_dict() for usage in trace.stage_usage]
    return {
        "mode": mode,
        "answer": answer.answer,
        "used_citations": list(answer.used_citations),
        "invalid_citations": list(answer.invalid_citations),
        "zero_valid_citation": answer.zero_valid_citation,
        "checks": {
            "chinese_chars": checks.chinese_chars,
            "in_target_range": checks.in_target_range,
            "duplicate_headings": list(checks.duplicate_headings),
            "repeated_conclusion_prefix": checks.repeated_conclusion_prefix,
            "has_valid_citation": checks.has_valid_citation,
            "passed": checks.passed,
        },
        # deep has no range, so there is nothing to report as one.
        "target_range": list(DEPTH_CHAR_RANGES.get(case.answer_depth, ())),
        "route": trace.route.to_dict(),
        "retry_count": trace.retry_count,
        "stop_reason": trace.stop_reason,
        "sufficiency": trace.final_sufficiency.to_dict(),
        "plan": trace.plan,
        "evidence_plan": trace.evidence_plan,
        "requirement_traces": list(trace.requirement_traces),
        "search_actions": list(trace.search_actions),
        "coverage_rounds": list(trace.coverage_rounds),
        "final_coverage": list(trace.final_coverage),
        "evidence_package_state": trace.evidence_package_state,
        "answer_plan": trace.answer_plan,
        "source_conflicts": list(trace.source_conflicts),
        "review": trace.review,
        "stages": stages,
        "usage": _usage_totals(stages),
        "context": _context_digest(result.context_bundle),
        "context_chars": result.context_bundle.total_chars,
        "context_max_chars": result.context_bundle.max_chars,
        "context_truncated": result.context_bundle.truncated,
        "error": None,
    }


def _failed_arm(mode: str, error: Exception) -> dict[str, Any]:
    return {
        "mode": mode,
        "answer": "",
        "used_citations": [],
        "invalid_citations": [],
        "zero_valid_citation": True,
        "checks": None,
        "route": None,
        "stages": [],
        "source_conflicts": [],
        "context": [],
        "error": f"{type(error).__name__}: {error}",
    }


def _judge_case(
    case: AnswerQualityCase,
    baseline_arm: dict[str, Any],
    candidate_arm: dict[str, Any],
    judge: PairwiseAnswerJudge,
    config: PairwiseEvaluationConfig,
) -> dict[str, Any]:
    if baseline_arm["error"] or candidate_arm["error"]:
        return {
            "winner": "ERROR",
            "first_order_winner": None,
            "swapped_order_winner": None,
            "reasons": [],
            "error": "judge skipped because an arm failed",
        }
    try:
        outcome = judge.judge(case, baseline_arm["answer"], candidate_arm["answer"])
    except Exception as exception:  # malformed judge JSON must not abort the batch
        return {
            "winner": "ERROR",
            "first_order_winner": None,
            "swapped_order_winner": None,
            "reasons": [],
            "error": f"{type(exception).__name__}: {exception}",
        }
    # Report in the caller's mode names; the judge only knows BASELINE / CANDIDATE.
    names = {
        CANONICAL_BASELINE: config.baseline_mode,
        CANONICAL_CANDIDATE: config.candidate_mode,
    }
    return {
        "winner": names.get(outcome.winner, outcome.winner),
        "first_order_winner": names.get(outcome.first_order_winner, outcome.first_order_winner),
        "swapped_order_winner": names.get(outcome.swapped_order_winner, outcome.swapped_order_winner),
        "reasons": list(outcome.reasons),
        "error": None,
    }


def _review_worksheet(
    case: AnswerQualityCase, record: dict[str, Any], modes: tuple[str, str]
) -> dict[str, Any]:
    """Everything a human needs to adjudicate history-vs-code conflicts by hand."""
    arms = record["arms"]
    conflicts = []
    for mode in modes:
        for conflict in arms[mode].get("source_conflicts") or []:
            conflicts.append({"mode": mode, **conflict})
    document_sources = []
    for mode in modes:
        for item in arms[mode].get("context") or []:
            if item["source_type"] == "DOCUMENT":
                document_sources.append({"mode": mode, **item})
    return {
        "known_conflicts": list(case.known_conflicts),
        "must_not_claim": list(case.must_not_claim),
        "reported_conflicts": conflicts,
        "document_evidence_in_context": document_sources,
        "needs_human_review": bool(case.known_conflicts) or bool(conflicts),
    }


def run_case(
    case: AnswerQualityCase,
    *,
    workflow_factory: WorkflowFactory,
    judge_client_factory: Callable[[], LLMClient],
    judge: PairwiseAnswerJudge | None = None,
    top_k: int = DEFAULT_TOP_K,
    config: PairwiseEvaluationConfig = DEFAULT_EVALUATION_CONFIG,
    pedagogy_judge: PedagogyJudge | None = None,
) -> dict[str, Any]:
    arms: dict[str, dict[str, Any]] = {}
    for mode in config.modes:
        try:
            workflow = workflow_factory(mode, case.answer_depth)
            arms[mode] = _capture_arm(case, mode, workflow.run(case.question, top_k))
        except Exception as exception:  # one bad case must not abort the batch
            arms[mode] = _failed_arm(mode, exception)
    judge = judge or PairwiseAnswerJudge(judge_client_factory)
    record: dict[str, Any] = {
        "id": case.id,
        "question": case.question,
        "answer_depth": case.answer_depth,
        "arms": arms,
        "judge": _judge_case(
            case, arms[config.baseline_mode], arms[config.candidate_mode], judge, config
        ),
    }
    record["review_worksheet"] = _review_worksheet(case, record, config.modes)
    # Scored on the candidate only: it is the arm under evaluation.
    candidate_arm = arms[config.candidate_mode]
    record["pedagogy"] = (
        pedagogy_judge.judge(case, candidate_arm["answer"]).to_dict()
        if pedagogy_judge is not None and not candidate_arm["error"]
        else None
    )
    return record


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = min(int(round(fraction * (len(ordered) - 1))), len(ordered) - 1)
    return round(ordered[position], 1)


def _arm_metrics(records: Sequence[dict[str, Any]], mode: str) -> dict[str, Any]:
    arms = [record["arms"][mode] for record in records]
    ok = [arm for arm in arms if not arm["error"]]
    latencies = [arm["usage"]["latency_ms"] for arm in ok]
    return {
        "case_count": len(arms),
        "failed": len(arms) - len(ok),
        "errors": [arm["error"] for arm in arms if arm["error"]],
        "checks_passed": sum(1 for arm in ok if arm["checks"]["passed"]),
        "in_target_range": sum(1 for arm in ok if arm["checks"]["in_target_range"]),
        "repeated_conclusion_prefix": sum(
            1 for arm in ok if arm["checks"]["repeated_conclusion_prefix"]
        ),
        "duplicate_headings": sum(1 for arm in ok if arm["checks"]["duplicate_headings"]),
        "zero_valid_citation": sum(1 for arm in ok if arm["zero_valid_citation"]),
        "invalid_citations": sum(
            len(arm["invalid_citations"]) for arm in ok
        ),
        "latency_ms": {
            "mean": _mean(latencies),
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "total": round(sum(latencies), 1),
        },
        "llm_calls": sum(arm["usage"]["llm_calls"] for arm in ok),
        "input_tokens": sum(arm["usage"]["input_tokens"] for arm in ok),
        "output_tokens": sum(arm["usage"]["output_tokens"] for arm in ok),
        "stages_reporting_usage": sum(
            arm["usage"]["stages_reporting_usage"] for arm in ok
        ),
    }


def _stage_metrics(records: Sequence[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        for stage in record["arms"][mode]["stages"]:
            buckets.setdefault(stage["stage"], []).append(stage)
    summary = []
    for stage, entries in sorted(buckets.items()):
        latencies = [float(entry["latency_ms"]) for entry in entries]
        summary.append(
            {
                "stage": stage,
                "calls": len(entries),
                "latency_ms_mean": _mean(latencies),
                "latency_ms_p95": _percentile(latencies, 0.95),
                "input_tokens": sum(int(entry.get("input_tokens") or 0) for entry in entries),
                "output_tokens": sum(int(entry.get("output_tokens") or 0) for entry in entries),
            }
        )
    return summary


def _acceptance(
    records: Sequence[dict[str, Any]],
    arms: dict[str, Any],
    config: PairwiseEvaluationConfig,
) -> list[dict[str, Any]]:
    winners = Counter(record["judge"]["winner"] for record in records)
    decisive = winners[config.baseline_mode] + winners[config.candidate_mode]
    win_rate = winners[config.candidate_mode] / decisive if decisive else None

    detailed = [record for record in records if record["answer_depth"] == "detailed"]
    detailed_in_range = sum(
        1
        for record in detailed
        if record["arms"][config.candidate_mode]["checks"] is not None
        and record["arms"][config.candidate_mode]["checks"]["in_target_range"]
    )
    detailed_rate = detailed_in_range / len(detailed) if detailed else None

    return [
        {
            "name": "v2_blind_win_rate_at_least_70pct",
            "passed": None if win_rate is None else win_rate >= WIN_RATE_THRESHOLD,
            "current": win_rate,
            "target": WIN_RATE_THRESHOLD,
            "detail": f"{winners[config.candidate_mode]} wins / {decisive} decisive",
        },
        {
            "name": "detailed_in_2200_5000_at_least_80pct",
            "passed": None if detailed_rate is None else detailed_rate >= DETAILED_LENGTH_THRESHOLD,
            "current": detailed_rate,
            "target": DETAILED_LENGTH_THRESHOLD,
            "detail": f"{detailed_in_range} of {len(detailed)} detailed cases",
        },
        {
            "name": "explain_avoids_repeated_conclusion_template",
            "passed": arms[config.candidate_mode]["repeated_conclusion_prefix"] == 0,
            "current": arms[config.candidate_mode]["repeated_conclusion_prefix"],
            "target": 0,
            "detail": "cases where '结论：' appears more than once",
        },
        {
            "name": "no_history_overrides_current_code",
            "passed": None,
            "current": sum(
                1 for record in records if record["review_worksheet"]["needs_human_review"]
            ),
            "target": "human review",
            "detail": "requires the conflict-review worksheet to be adjudicated by hand",
        },
        {
            "name": "factual_correctness_not_below_v1",
            "passed": None,
            "current": None,
            "target": "human review",
            "detail": "compare must_cover / must_not_claim per case by hand",
        },
        {
            "name": "no_evaluation_errors",
            "passed": None,
            "current": (
                arms[config.baseline_mode]["failed"]
                + arms[config.candidate_mode]["failed"]
                + winners["ERROR"]
                + winners["POSITION_BIASED"]
            ),
            "target": 0,
            "detail": "failed arms + judge errors + position-biased cases, reported not gated",
        },
    ]


def summarise(
    records: Sequence[dict[str, Any]],
    config: PairwiseEvaluationConfig = DEFAULT_EVALUATION_CONFIG,
) -> dict[str, Any]:
    winners = Counter(record["judge"]["winner"] for record in records)
    arms = {mode: _arm_metrics(records, mode) for mode in config.modes}
    return {
        "case_count": len(records),
        "modes": {"baseline": config.baseline_mode, "candidate": config.candidate_mode},
        "judge_winners": dict(winners),
        "arms": arms,
        "stages": {mode: _stage_metrics(records, mode) for mode in config.modes},
        "acceptance": _acceptance(records, arms, config),
        "pedagogy": summarise_pedagogy([
            PedagogyScore(scores=item["pedagogy"]["scores"], notes=tuple(item["pedagogy"]["notes"]),
                          decision_source=item["pedagogy"]["decision_source"],
                          error=item["pedagogy"]["error"])
            for item in records
            if item.get("pedagogy") is not None
        ]),
        "human_review_queue": [
            {
                "id": record["id"],
                "answer_depth": record["answer_depth"],
                "judge_winner": record["judge"]["winner"],
                "known_conflicts": record["review_worksheet"]["known_conflicts"],
                "reported_conflicts": record["review_worksheet"]["reported_conflicts"],
            }
            for record in records
            if record["review_worksheet"]["needs_human_review"]
        ],
    }


def run_answer_quality_evaluation(
    *,
    cases_path: Path,
    workflow_factory: WorkflowFactory,
    judge_client_factory: Callable[[], LLMClient],
    top_k: int = DEFAULT_TOP_K,
    only: Sequence[str] | None = None,
    limit: int | None = None,
    journal_path: Path | None = None,
    resume: bool = False,
    progress: Callable[[str], None] | None = None,
    config: PairwiseEvaluationConfig = DEFAULT_EVALUATION_CONFIG,
    pedagogy_judge_factory: Callable[[], LLMClient] | None = None,
) -> dict[str, Any]:
    say = progress or (lambda message: None)
    cases = load_answer_quality_cases(cases_path)
    if only:
        wanted = list(dict.fromkeys(only))
        index = {case.id: case for case in cases}
        unknown = [identifier for identifier in wanted if identifier not in index]
        if unknown:
            raise ValueError(f"unknown answer quality case ids: {', '.join(unknown)}")
        cases = [index[identifier] for identifier in wanted]
    if limit is not None:
        cases = cases[:limit]

    done: dict[str, dict[str, Any]] = {}
    if resume and journal_path is not None and journal_path.is_file():
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing = json.loads(line)
                done[existing["id"]] = existing
        if done:
            say(f"resuming: {len(done)} case(s) already recorded in {journal_path.name}")

    records: list[dict[str, Any]] = []
    total = len(cases)
    for position, case in enumerate(cases, start=1):
        if case.id in done:
            records.append(done[case.id])
            continue
        say(f"[{position}/{total}] {case.id} ({case.answer_depth}) running both modes")
        record = run_case(
            case,
            workflow_factory=workflow_factory,
            judge_client_factory=judge_client_factory,
            top_k=top_k,
            config=config,
            pedagogy_judge=(
                PedagogyJudge(pedagogy_judge_factory)
                if pedagogy_judge_factory is not None
                else None
            ),
        )
        winner = record["judge"]["winner"]
        if record["judge"]["error"]:
            say(f"[{position}/{total}] {case.id} judge={winner} ({record['judge']['error']})")
        else:
            say(f"[{position}/{total}] {case.id} judge={winner}")
        if journal_path is not None:
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            with journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        records.append(record)

    summary = summarise(records, config)
    summary["generated_at"] = _now()
    summary["cases_path"] = str(cases_path)
    summary["top_k"] = top_k
    if journal_path is not None:
        summary["journal"] = str(journal_path)
    return {"summary": summary, "records": records}
