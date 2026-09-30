from __future__ import annotations

import pytest

from pathlib import Path

from devcontext.evaluation import (
    PairwiseAnswerJudge,
    check_answer_shape,
    load_answer_quality_cases,
)
from devcontext.evaluation.answer_quality import AnswerQualityCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_l2_dataset_has_the_planned_case_distribution() -> None:
    cases = load_answer_quality_cases(
        PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"
    )

    assert len(cases) == 19
    assert sum(case.id.startswith("flow-") for case in cases) == 6
    assert sum(case.id.startswith("why-") for case in cases) == 4
    assert sum(case.id.startswith("edge-") for case in cases) == 4
    assert sum(case.id.startswith("negative-") for case in cases) == 2
    assert sum(case.id.startswith("locate-") for case in cases) == 2
    assert sum(case.id.startswith("teach-") for case in cases) == 1
    consistency = next(case for case in cases if case.id == "flow-01")
    assert "MySQL 条件更新的最终正确性边界" in consistency.must_cover
    assert any("WHERE seat_status = AVAILABLE" in item for item in consistency.must_not_claim)

    # The V3 golden case. These two claims are the ones this project got right and
    # the reference answer got wrong; they must not be quietly dropped.
    golden = next(case for case in cases if case.id == "teach-token-bucket-01")
    assert golden.answer_depth == "detailed"
    assert any("字段级缺失判定" in item for item in golden.must_not_claim)
    assert any("全部席别" in item for item in golden.must_not_claim)


def test_answer_shape_check_catches_mechanical_repetition() -> None:
    # Pinned by id: depending on the last case in the file made this test silently
    # require that case to be a `brief` one with a 150-500 character range.
    case = next(
        case
        for case in load_answer_quality_cases(
            PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"
        )
        if case.id == "locate-02"
    )
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

    # The judge speaks in BASELINE / CANDIDATE; the runner maps those onto the
    # caller's mode names.
    result = PairwiseAnswerJudge(lambda: FakeClient()).judge(case, "baseline", "candidate")

    assert result.first_order_winner == "BASELINE"
    assert result.swapped_order_winner == "BASELINE"
    assert result.winner == "BASELINE"


def test_every_checked_in_case_loads_after_the_schema_extension() -> None:
    """Teaching fields were added after eighteen cases were already frozen."""
    cases = load_answer_quality_cases(
        PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"
    )

    assert len(cases) == 19
    for case in cases:
        assert isinstance(case.core_mental_model, tuple)
        assert isinstance(case.must_explain_why, tuple)


def test_cases_predating_the_teaching_fields_still_carry_the_originals() -> None:
    cases = {
        case.id: case
        for case in load_answer_quality_cases(
            PROJECT_ROOT / "benchmark" / "l2-answer-quality.jsonl"
        )
    }
    flow = cases["flow-01"]

    assert flow.core_mental_model == ()
    assert flow.has_teaching_expectations is False
    # The fields that were always there are untouched.
    assert "MySQL 条件更新的最终正确性边界" in flow.must_cover


def test_teaching_fields_load_when_present() -> None:
    payload = {
        "id": "x", "question": "q", "answer_depth": "detailed",
        "must_cover": ["a"], "must_not_claim": ["b"], "required_evidence": ["c"],
        "expected_explanation_shape": "d", "known_conflicts": [],
        "core_mental_model": ["Redis 是准入层"],
        "must_explain_why": ["为什么放在锁之前"],
        "useful_scenarios": ["库存不足"],
        "misconceptions": ["把桶当库存"],
        "pedagogy_expectations": ["先立问题"],
    }

    case = AnswerQualityCase.from_dict(payload)

    assert case.core_mental_model == ("Redis 是准入层",)
    assert case.has_teaching_expectations is True


def test_an_unknown_field_is_still_rejected() -> None:
    """Relaxing the loader must not turn it into a free-for-all."""
    payload = {
        "id": "x", "question": "q", "answer_depth": "detailed",
        "must_cover": ["a"], "must_not_claim": ["b"], "required_evidence": ["c"],
        "expected_explanation_shape": "d", "known_conflicts": [],
        "core_mental_model_typo": ["oops"],
    }

    with pytest.raises(ValueError, match="invalid fields"):
        AnswerQualityCase.from_dict(payload)


def test_a_deep_case_has_no_character_ceiling() -> None:
    payload = {
        "id": "x", "question": "q", "answer_depth": "deep",
        "must_cover": ["a"], "must_not_claim": ["b"], "required_evidence": ["c"],
        "expected_explanation_shape": "d", "known_conflicts": [],
    }
    case = AnswerQualityCase.from_dict(payload)

    checks = check_answer_shape(case, "字" * 20_000, ["E1"])

    assert checks.in_target_range is True, "deep must not be measured against a ceiling"
    assert checks.chinese_chars == 20_000
