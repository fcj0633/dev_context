from __future__ import annotations

import json

import pytest

from devcontext.evaluation.pedagogy import (
    DIMENSIONS,
    PedagogyJudge,
    PedagogyScore,
    summarise_pedagogy,
)
from devcontext.evaluation.answer_quality import AnswerQualityCase


def payload(**overrides) -> str:
    value = {name: 3 for name in DIMENSIONS}
    value["notes"] = ["结构清楚，但失败推演偏薄"]
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


class ScriptedClient:
    last_usage: dict = {}
    model = "fake"
    reasoning_effort = "high"

    def __init__(self, response: str) -> None:
        self.response = response

    def generate(self, messages):
        return self.response


def case() -> AnswerQualityCase:
    return AnswerQualityCase.from_dict({
        "id": "teach-x", "question": "q", "answer_depth": "detailed",
        "must_cover": ["a"], "must_not_claim": ["b"], "required_evidence": ["c"],
        "expected_explanation_shape": "d", "known_conflicts": [],
        "core_mental_model": ["Redis 是准入层"],
        "must_explain_why": ["为什么放在锁之前"],
    })


class TestPedagogyScore:
    def test_parses_every_dimension(self) -> None:
        score = PedagogyJudge(lambda: ScriptedClient(payload())).judge(case(), "答案")

        assert score.decision_source == "llm"
        assert score.overall == 3.0
        assert score.notes == ("结构清楚，但失败推演偏薄",)

    def test_overall_is_the_mean_of_the_dimensions(self) -> None:
        score = PedagogyJudge(lambda: ScriptedClient(payload(
            mental_model=5, causal_explanation=5, progressive_disclosure=5,
            examples=0, failure_reasoning=0, tradeoffs=0, readability=5,
        ))).judge(case(), "答案")

        assert score.overall == pytest.approx(20 / 7, abs=1e-3)

    def test_scores_must_be_integers_within_range(self) -> None:
        reviewer = lambda text: PedagogyJudge(lambda: ScriptedClient(text))  # noqa: E731

        for bad in (payload(mental_model=6), payload(mental_model=-1), payload(mental_model=2.5)):
            result = reviewer(bad).judge(case(), "答案")
            assert result.decision_source == "fallback", bad
            assert result.error is not None

    def test_a_missing_dimension_is_rejected(self) -> None:
        incomplete = {name: 3 for name in DIMENSIONS if name != "tradeoffs"}
        incomplete["notes"] = []

        result = PedagogyJudge(
            lambda: ScriptedClient(json.dumps(incomplete, ensure_ascii=False))
        ).judge(case(), "答案")

        assert result.decision_source == "fallback"

    def test_a_failing_call_does_not_raise(self) -> None:
        class Boom:
            last_usage: dict = {}
            model = "fake"
            reasoning_effort = "high"

            def generate(self, messages):
                raise RuntimeError("network down")

        result = PedagogyJudge(lambda: Boom()).judge(case(), "答案")

        assert result.overall is None
        assert "network down" in (result.error or "")

    def test_an_unscored_result_has_no_overall(self) -> None:
        assert PedagogyScore().overall is None


class TestPedagogySummary:
    def test_means_over_the_cases_that_scored(self) -> None:
        scores = [
            PedagogyScore(scores={name: 4 for name in DIMENSIONS}),
            PedagogyScore(scores={name: 2 for name in DIMENSIONS}),
            PedagogyScore(decision_source="fallback", error="boom"),
        ]

        summary = summarise_pedagogy(scores)

        assert summary["scored_cases"] == 2
        assert summary["errors"] == 1
        assert summary["mean"] == {name: 3.0 for name in DIMENSIONS}
        assert summary["overall"] == 3.0

    def test_no_scored_cases_is_reported_not_faked(self) -> None:
        summary = summarise_pedagogy([PedagogyScore(error="boom")])

        assert summary["scored_cases"] == 0
        assert summary["mean"] == {}

    def test_the_payload_does_not_carry_the_answer_of_an_arm(self) -> None:
        """The judge scores one answer; it is not told which mode produced it."""
        seen: list[dict] = []

        class Recording(ScriptedClient):
            def generate(self, messages):
                seen.append(json.loads(messages[-1].content))
                return self.response

        PedagogyJudge(lambda: Recording(payload())).judge(case(), "答案")

        assert set(seen[0]) == {
            "question", "expected_mental_model", "must_explain_why",
            "useful_scenarios", "misconceptions_to_correct",
            "pedagogy_expectations", "answer",
        }
