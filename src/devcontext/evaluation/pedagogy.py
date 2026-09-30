from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from devcontext.evaluation.answer_quality import AnswerQualityCase
from devcontext.llm import LLMClient, LLMMessage


PEDAGOGY_MAX_TOKENS = 16_384
MAX_SCORE = 5

DIMENSIONS = (
    "mental_model",
    "causal_explanation",
    "progressive_disclosure",
    "examples",
    "failure_reasoning",
    "tradeoffs",
    "readability",
)

PEDAGOGY_SYSTEM_PROMPT = f"""你是 DevContext-Java 的教学效果评审。只评价"讲清楚了没有"，不评事实对错（那是另一个评审的职责）。

对每个维度打 0–{MAX_SCORE} 分：
- mental_model：有没有一条贯穿全文、能被读者复述的核心模型？
- causal_explanation：关键设计有没有解释"为什么"，而不只是"是什么"？
- progressive_disclosure：是否由浅入深，先给全貌再给细节？
- examples：示例与类比是否真的帮助理解，且明确标为假设？
- failure_reasoning：有没有推演失败情形与边界？
- tradeoffs：有没有说明取舍与代价，而不是只讲优点？
- readability：结构是否清晰、术语是否一致、有没有不必要的堆砌？

不要因为答案更长就给更高分。分数为整数 0–{MAX_SCORE}。
只输出严格 JSON：
{{"mental_model": 0, "causal_explanation": 0, "progressive_disclosure": 0,
 "examples": 0, "failure_reasoning": 0, "tradeoffs": 0, "readability": 0,
 "notes": ["简短说明"]}}"""


@dataclass(frozen=True, slots=True)
class PedagogyScore:
    scores: dict[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    decision_source: str = "llm"
    error: str | None = None

    @property
    def overall(self) -> float | None:
        values = [self.scores.get(name) for name in DIMENSIONS]
        if any(value is None for value in values):
            return None
        return round(sum(values) / len(DIMENSIONS), 3)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scores": {name: self.scores.get(name) for name in DIMENSIONS},
            "overall": self.overall,
            "notes": list(self.notes),
            "decision_source": self.decision_source,
            "error": self.error,
        }


class PedagogyJudge:
    """Scores how well an answer teaches, on dimensions the L2 gate does not see."""

    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory

    def judge(self, case: AnswerQualityCase, answer: str) -> PedagogyScore:
        payload = {
            "question": case.question,
            "expected_mental_model": list(case.core_mental_model),
            "must_explain_why": list(case.must_explain_why),
            "useful_scenarios": list(case.useful_scenarios),
            "misconceptions_to_correct": list(case.misconceptions),
            "pedagogy_expectations": list(case.pedagogy_expectations),
            "answer": answer,
        }
        try:
            response = self.llm_client_factory().generate([
                LLMMessage("system", PEDAGOGY_SYSTEM_PROMPT),
                LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
            ])
            return _parse(response)
        except Exception as exception:
            from devcontext.agentic.models import error_detail

            return PedagogyScore(decision_source="fallback", error=error_detail(exception))


def _parse(response: str) -> PedagogyScore:
    value = json.loads(response)
    if not isinstance(value, dict) or set(value) != {*DIMENSIONS, "notes"}:
        raise ValueError("pedagogy score has invalid fields")
    scores: dict[str, int] = {}
    for name in DIMENSIONS:
        raw = value[name]
        if not isinstance(raw, int) or isinstance(raw, bool) or not 0 <= raw <= MAX_SCORE:
            raise ValueError(f"pedagogy score {name} is invalid")
        scores[name] = raw
    notes = value["notes"]
    if not isinstance(notes, list) or any(
        not isinstance(item, str) for item in notes
    ):
        raise ValueError("pedagogy notes must be a string list")
    return PedagogyScore(scores=scores, notes=tuple(notes))


def summarise_pedagogy(scores: list[PedagogyScore]) -> dict[str, Any]:
    usable = [item for item in scores if item.overall is not None]
    if not usable:
        return {"scored_cases": 0, "errors": len(scores), "mean": {}}
    means = {
        name: round(
            sum(item.scores[name] for item in usable) / len(usable), 3
        )
        for name in DIMENSIONS
    }
    return {
        "scored_cases": len(usable),
        "errors": len(scores) - len(usable),
        "mean": means,
        "overall": round(sum(means.values()) / len(DIMENSIONS), 3),
    }
