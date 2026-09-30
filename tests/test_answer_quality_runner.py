from __future__ import annotations

import json
from pathlib import Path

import pytest

from devcontext.agentic.models import (
    AgenticAnswerResult,
    AgenticTrace,
    StageUsage,
    SufficiencyResult,
)
from devcontext.evaluation import load_answer_quality_cases
from devcontext.evaluation.answer_quality_runner import (
    V1_MODE,
    V2_MODE,
    PairwiseEvaluationConfig,
    run_answer_quality_evaluation,
    run_case,
    summarise,
)
from devcontext.evaluation.pedagogy import DIMENSIONS, PedagogyScore
from devcontext.models import AnswerResult, Citation, ContextBundle, ContextItem
from devcontext.routing import DecisionSource, QueryType, RouteDecision


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES = PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"

CASES_CACHE = load_answer_quality_cases(CASES)

CHINESE_150 = "字" * 150
CHINESE_3000 = "字" * 3000
MARKER = "EXPLAIN_BETTER"


def case(case_id: str):
    return next(item for item in CASES_CACHE if item.id == case_id)


class ContentJudgeClient:
    """Order-independent judge: prefers whichever slot holds the marked answer.

    `PairwiseAnswerJudge` builds a fresh client per call, so the fake has to decide from
    the payload rather than from a queued response.
    """

    def __init__(self, marked_answer_contains: str = MARKER) -> None:
        self.marked = marked_answer_contains

    def generate(self, messages) -> str:
        payload = json.loads(messages[1].content)
        winner = "A" if self.marked in payload["answer_a"] else "B"
        return json.dumps({"winner": winner, "reason": "content based"})


class AlwaysFirstClient:
    """Always prefers slot A, which is the position-bias signature."""

    def generate(self, messages) -> str:
        return json.dumps({"winner": "A", "reason": "first slot"})


def _context_bundle() -> ContextBundle:
    citation = Citation(
        label="C1",
        source_type="DOCUMENT",
        file_path="design.md",
        heading_path=["设计"],
    )
    item = ContextItem(
        citation=citation,
        content="evidence",
        chunk_id=4242,
        chunk_type="DOCUMENT_SECTION",
        score=1.0,
        retrieval_rank=1,
        source_role="HISTORICAL_PLAN",
        temporal_status="HISTORICAL",
        sub_question_ids=["SQ1"],
    )
    return ContextBundle(
        query="q",
        items=[item],
        rendered_text="rendered",
        total_chars=10,
        max_chars=8000,
        truncated=False,
    )


def _trace(depth: str = "detailed") -> AgenticTrace:
    return AgenticTrace(
        route=RouteDecision(QueryType.MIXED, DecisionSource.RULES, "rules"),
        rounds=[],
        retry_count=1,
        final_sufficiency=SufficiencyResult(True, (), "ok", "llm"),
        stop_reason="sufficient",
        plan={"answer_depth": depth},
        stage_usage=[
            StageUsage("investigation_planning", 100.0, "llm", "deepseek-flash", "high", 10, 20),
            StageUsage("grounded_draft", 900.0, "llm", "deepseek-flash", "high", 100, 200),
            StageUsage("evidence_retrieval", 5.0, "policy"),
        ],
    )


class FakeWorkflow:
    def __init__(self, answer: str, depth: str) -> None:
        self.answer = answer
        self.depth = depth

    def run(self, query: str, top_k: int) -> AgenticAnswerResult:
        return AgenticAnswerResult(
            AnswerResult(answer=self.answer, used_citations=["C1"]),
            _context_bundle(),
            _trace(self.depth),
        )


def _factory(answers: dict[str, str], seen: list[tuple[str, str]] | None = None):
    def factory(mode: str, depth: str):
        if seen is not None:
            seen.append((mode, depth))
        return FakeWorkflow(answers[mode], depth)

    return factory


def test_run_case_records_both_arms_and_a_v2_verdict() -> None:
    seen: list[tuple[str, str]] = []

    record = run_case(
        case("locate-01"),
        workflow_factory=_factory(
            {
                V1_MODE: "结论：位置如下。" + CHINESE_150,
                V2_MODE: "入口在 UserController。" + MARKER + CHINESE_150,
            },
            seen,
        ),
        judge_client_factory=lambda: ContentJudgeClient(),
    )

    # the depth is forced to the case's declared depth for both arms
    assert seen == [(V1_MODE, "brief"), (V2_MODE, "brief")]
    assert record["judge"]["winner"] == V2_MODE
    assert record["judge"]["first_order_winner"] == V2_MODE
    assert record["judge"]["swapped_order_winner"] == V2_MODE
    assert record["arms"][V2_MODE]["error"] is None
    assert record["arms"][V2_MODE]["usage"]["llm_calls"] == 2
    assert record["arms"][V2_MODE]["usage"]["input_tokens"] == 110
    assert record["arms"][V2_MODE]["usage"]["output_tokens"] == 220
    # locate-01 declares no known conflicts, so nothing is queued for human review
    assert record["review_worksheet"]["needs_human_review"] is False
    assert (
        record["review_worksheet"]["document_evidence_in_context"][0]["temporal_status"]
        == "HISTORICAL"
    )
    assert record["review_worksheet"]["document_evidence_in_context"][0]["chunk_id"] == 4242


def test_review_worksheet_flags_cases_with_known_conflicts() -> None:
    conflicting = next(item for item in CASES_CACHE if item.known_conflicts)

    record = run_case(
        conflicting,
        workflow_factory=_factory({V1_MODE: "a", V2_MODE: "b"}),
        judge_client_factory=lambda: ContentJudgeClient(),
    )

    assert record["review_worksheet"]["needs_human_review"] is True
    assert list(conflicting.known_conflicts) == record["review_worksheet"]["known_conflicts"]
    assert list(conflicting.must_not_claim) == record["review_worksheet"]["must_not_claim"]
    queue = summarise([record])["human_review_queue"]
    assert [entry["id"] for entry in queue] == [conflicting.id]


def test_run_case_isolates_a_failing_arm() -> None:
    def factory(mode: str, depth: str):
        if mode == V2_MODE:
            raise RuntimeError("DeepSeek generation did not complete: finish_reason=length")
        return FakeWorkflow("入口在 UserController。" + CHINESE_150, depth)

    record = run_case(
        case("locate-01"),
        workflow_factory=factory,
        judge_client_factory=lambda: ContentJudgeClient(),
    )

    assert record["arms"][V2_MODE]["error"] is not None
    assert "finish_reason=length" in record["arms"][V2_MODE]["error"]
    assert record["arms"][V1_MODE]["error"] is None
    # The judge is skipped rather than allowed to compare against an empty answer.
    assert record["judge"]["winner"] == "ERROR"


def test_position_bias_is_reported_and_excluded_from_the_win_rate() -> None:
    record = run_case(
        case("locate-01"),
        workflow_factory=_factory({V1_MODE: "a" + CHINESE_150, V2_MODE: "b" + CHINESE_150}),
        judge_client_factory=lambda: AlwaysFirstClient(),
    )

    assert record["judge"]["winner"] == "POSITION_BIASED"
    summary = summarise([record])
    assert summary["judge_winners"] == {"POSITION_BIASED": 1}
    gate = next(
        item
        for item in summary["acceptance"]
        if item["name"] == "v2_blind_win_rate_at_least_70pct"
    )
    assert gate["passed"] is None
    assert gate["current"] is None


def test_summary_computes_the_win_rate_over_decisive_cases() -> None:
    located = [item for item in CASES_CACHE if item.id.startswith("locate-")]
    records = [
        run_case(
            item,
            workflow_factory=_factory(
                {
                    V1_MODE: "v1" + (" " + MARKER if mark_v1 else ""),
                    V2_MODE: "v2" + ("" if mark_v1 else " " + MARKER),
                }
            ),
            judge_client_factory=lambda: ContentJudgeClient(),
        )
        for item, mark_v1 in zip(located, (False, True))
    ]

    summary = summarise(records)

    assert summary["judge_winners"] == {V2_MODE: 1, V1_MODE: 1}
    gate = next(
        item
        for item in summary["acceptance"]
        if item["name"] == "v2_blind_win_rate_at_least_70pct"
    )
    assert gate["current"] == 0.5
    assert gate["passed"] is False


def test_detailed_length_gate_uses_the_declared_depth() -> None:
    detailed = next(item for item in CASES_CACHE if item.answer_depth == "detailed")

    def evaluate(answer: str):
        return summarise([
            run_case(
                detailed,
                workflow_factory=_factory({V1_MODE: "a", V2_MODE: answer + MARKER}),
                judge_client_factory=lambda: ContentJudgeClient(),
            )
        ])

    def gate(summary):
        return next(
            item
            for item in summary["acceptance"]
            if item["name"] == "detailed_in_2200_5000_at_least_80pct"
        )

    assert gate(evaluate("短的" + CHINESE_150))["passed"] is False
    assert gate(evaluate("长的" + CHINESE_3000))["passed"] is True


def test_explain_template_gate_counts_repeated_conclusion_prefix() -> None:
    def evaluate(answer: str):
        return summarise([
            run_case(
                case("locate-01"),
                workflow_factory=_factory({V1_MODE: "a", V2_MODE: answer}),
                judge_client_factory=lambda: ContentJudgeClient(),
            )
        ])

    def gate(summary):
        return next(
            item
            for item in summary["acceptance"]
            if item["name"] == "explain_avoids_repeated_conclusion_template"
        )

    assert gate(evaluate("结论：一。结论：二。" + CHINESE_150))["passed"] is False
    assert gate(evaluate("一句话说清。" + CHINESE_150))["passed"] is True


def test_only_rejects_unknown_ids() -> None:
    with pytest.raises(ValueError, match="unknown answer quality case ids"):
        run_answer_quality_evaluation(
            cases_path=CASES,
            workflow_factory=_factory({V1_MODE: "a", V2_MODE: "b"}),
            judge_client_factory=lambda: ContentJudgeClient(),
            only=["nope-01"],
        )


def test_journal_is_written_and_resumed(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    calls: list[str] = []

    def factory(mode: str, depth: str):
        calls.append(mode)
        return FakeWorkflow("答案" + CHINESE_150, depth)

    first = run_answer_quality_evaluation(
        cases_path=CASES,
        workflow_factory=factory,
        judge_client_factory=lambda: ContentJudgeClient(),
        only=["locate-01", "locate-02"],
        journal_path=journal,
    )
    assert first["summary"]["case_count"] == 2
    assert len(calls) == 4
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2

    calls.clear()
    without_resume = run_answer_quality_evaluation(
        cases_path=CASES,
        workflow_factory=factory,
        judge_client_factory=lambda: ContentJudgeClient(),
        only=["locate-01", "locate-02"],
        journal_path=journal,
    )
    # the journal is only read when resume is requested
    assert without_resume["summary"]["case_count"] == 2
    assert calls == [V1_MODE, V2_MODE, V1_MODE, V2_MODE]

    calls.clear()
    resumed = run_answer_quality_evaluation(
        cases_path=CASES,
        workflow_factory=factory,
        judge_client_factory=lambda: ContentJudgeClient(),
        only=["locate-01", "locate-02"],
        journal_path=journal,
        resume=True,
    )
    assert resumed["summary"]["case_count"] == 2
    assert calls == []  # both cases came back from the journal


def test_runner_accepts_an_arbitrary_mode_pair() -> None:
    """The runner must not be hard-wired to legacy vs explain."""
    seen: list[tuple[str, str]] = []

    record = run_case(
        case("locate-01"),
        workflow_factory=_factory(
            {
                "explain": "结论：位置如下。" + CHINESE_150,
                "teach": "入口在 UserController。" + MARKER + CHINESE_150,
            },
            seen,
        ),
        judge_client_factory=lambda: ContentJudgeClient(),
        config=PairwiseEvaluationConfig("explain", "teach"),
    )

    assert seen == [("explain", "brief"), ("teach", "brief")]
    assert set(record["arms"]) == {"explain", "teach"}
    assert record["judge"]["winner"] == "teach"
    assert record["arms"]["teach"]["error"] is None


def test_judge_winners_are_reported_in_the_callers_mode_names() -> None:
    config = PairwiseEvaluationConfig("legacy", "explain")

    record = run_case(
        case("locate-01"),
        workflow_factory=_factory(
            {
                V1_MODE: "结论：位置如下。" + CHINESE_150,
                V2_MODE: "入口在 UserController。" + MARKER + CHINESE_150,
            },
        ),
        judge_client_factory=lambda: ContentJudgeClient(),
        config=config,
    )
    summary = summarise([record], config)

    assert summary["modes"] == {"baseline": "legacy", "candidate": "explain"}
    assert set(summary["arms"]) == {"legacy", "explain"}
    assert summary["judge_winners"] == {"explain": 1}


def test_the_judge_payload_carries_no_mode_identity() -> None:
    """The comparison stays blind: two anonymous answers and the case's criteria."""
    payloads: list[dict] = []

    class RecordingJudge(ContentJudgeClient):
        def generate(self, messages):
            payloads.append(json.loads(messages[-1].content))
            return super().generate(messages)

    run_case(
        case("locate-01"),
        workflow_factory=_factory(
            {
                "explain": "结论：位置如下。" + CHINESE_150,
                "teach": "入口在 UserController。" + MARKER + CHINESE_150,
            },
        ),
        judge_client_factory=lambda: RecordingJudge(),
        config=PairwiseEvaluationConfig("explain", "teach"),
    )

    assert len(payloads) == 2, "one call per order"
    for payload in payloads:
        assert set(payload) == {
            "question", "must_cover", "must_not_claim", "required_evidence",
            "expected_explanation_shape", "known_conflicts", "expected_mental_model",
            "must_explain_why", "useful_scenarios", "misconceptions_to_correct",
            "pedagogy_expectations", "answer_a", "answer_b",
        }
    # The two orders swap which answer sits in position A.
    assert payloads[0]["answer_a"] != payloads[1]["answer_a"]


def test_pedagogy_is_scored_on_the_candidate_only() -> None:
    scored: list[str] = []

    class RecordingPedagogy:
        def judge(self, case, answer):
            scored.append(answer)
            return PedagogyScore(scores={name: 4 for name in DIMENSIONS})

    record = run_case(
        case("locate-01"),
        workflow_factory=_factory(
            {
                V1_MODE: "结论：位置如下。" + CHINESE_150,
                V2_MODE: "入口在 UserController。" + MARKER + CHINESE_150,
            },
        ),
        judge_client_factory=lambda: ContentJudgeClient(),
        pedagogy_judge=RecordingPedagogy(),
    )

    assert len(scored) == 1
    assert MARKER in scored[0], "the candidate arm is the one under evaluation"
    assert record["pedagogy"]["overall"] == 4.0

    summary = summarise([record])
    assert summary["pedagogy"]["scored_cases"] == 1
    assert summary["pedagogy"]["overall"] == 4.0


def test_pedagogy_is_skipped_when_the_candidate_arm_failed() -> None:
    class Boom:
        def judge(self, case, answer):
            raise AssertionError("must not be called")

    def factory(mode: str, depth: str):
        if mode == V2_MODE:
            raise RuntimeError("arm failed")
        return FakeWorkflow("结论：位置如下。" + CHINESE_150, depth)

    record = run_case(
        case("locate-01"),
        workflow_factory=factory,
        judge_client_factory=lambda: ContentJudgeClient(),
        pedagogy_judge=Boom(),
    )

    assert record["pedagogy"] is None
    assert summarise([record])["pedagogy"]["scored_cases"] == 0
