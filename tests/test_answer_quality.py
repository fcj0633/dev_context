from __future__ import annotations

from pathlib import Path

from devcontext.evaluation import (
    PairwiseAnswerJudge,
    check_answer_shape,
    load_answer_quality_cases,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_l2_dataset_has_the_planned_18_case_distribution() -> None:
    cases = load_answer_quality_cases(
        PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"
    )

    assert len(cases) == 18
    assert sum(case.id.startswith("flow-") for case in cases) == 6
    assert sum(case.id.startswith("why-") for case in cases) == 4
    assert sum(case.id.startswith("edge-") for case in cases) == 4
    assert sum(case.id.startswith("negative-") for case in cases) == 2
    assert sum(case.id.startswith("locate-") for case in cases) == 2
    consistency = next(case for case in cases if case.id == "flow-01")
    assert "MySQL 条件更新的最终正确性边界" in consistency.must_cover
    assert any("WHERE seat_status = AVAILABLE" in item for item in consistency.must_not_claim)


def test_answer_shape_check_catches_mechanical_repetition() -> None:
    case = load_answer_quality_cases(
        PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"
    )[-1]
    answer = "结论：位置如下。\n\n结论：重复。" + "字" * 150

    checks = check_answer_shape(case, answer, ["C1"])

    assert checks.in_target_range is True
    assert checks.repeated_conclusion_prefix is True
    assert checks.passed is False


def test_pairwise_judge_swaps_candidate_order() -> None:
    class FakeClient:
        responses = iter((
            '{"winner":"A","reason":"A 更完整"}',
            '{"winner":"B","reason":"B 更完整"}',
        ))

        def generate(self, messages) -> str:
            return next(self.responses)

    case = load_answer_quality_cases(
        PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"
    )[0]

    result = PairwiseAnswerJudge(lambda: FakeClient()).judge(case, "V1", "V2")

    assert result.first_order_winner == "V1"
    assert result.swapped_order_winner == "V1"
    assert result.winner == "V1"
