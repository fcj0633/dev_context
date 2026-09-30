from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable

from devcontext.llm import LLMClient, LLMMessage


DEPTH_CHAR_RANGES = {
    "brief": (150, 500),
    "standard": (800, 1800),
    "detailed": (2200, 5000),
}
# `deep` carries no range on purpose. The 2200-5000 window was the binding
# constraint on a hard question, not the model's window, so measuring a deep
# answer against a ceiling would reimpose the thing this round removed.
NO_UPPER_BOUND_DEPTHS = ("deep",)
ALL_DEPTHS = (*DEPTH_CHAR_RANGES, *NO_UPPER_BOUND_DEPTHS)

# Teaching fields, all optional so the cases that predate them keep loading.
TEACHING_FIELDS = (
    "core_mental_model",
    "must_explain_why",
    "useful_scenarios",
    "misconceptions",
    "pedagogy_expectations",
)


@dataclass(frozen=True, slots=True)
class AnswerQualityCase:
    id: str
    question: str
    answer_depth: str
    must_cover: tuple[str, ...]
    must_not_claim: tuple[str, ...]
    required_evidence: tuple[str, ...]
    expected_explanation_shape: str
    known_conflicts: tuple[str, ...]
    # Teaching dimensions, added after the first eighteen cases were frozen.
    # Optional with empty defaults so those cases keep loading unchanged; the
    # dataset and its loader must not change shape at the same time.
    core_mental_model: tuple[str, ...] = ()
    must_explain_why: tuple[str, ...] = ()
    useful_scenarios: tuple[str, ...] = ()
    misconceptions: tuple[str, ...] = ()
    pedagogy_expectations: tuple[str, ...] = ()

    @property
    def has_teaching_expectations(self) -> bool:
        return bool(
            self.core_mental_model
            or self.must_explain_why
            or self.useful_scenarios
            or self.misconceptions
            or self.pedagogy_expectations
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnswerQualityCase":
        required = {
            "id", "question", "answer_depth", "must_cover", "must_not_claim",
            "required_evidence", "expected_explanation_shape", "known_conflicts",
        }
        unknown = set(value) - required - set(TEACHING_FIELDS)
        if not isinstance(value, dict) or not required <= set(value) or unknown:
            raise ValueError("answer quality case has invalid fields")
        depth = value["answer_depth"]
        if depth not in ALL_DEPTHS:
            raise ValueError("answer quality case has invalid depth")
        text_fields = ("id", "question", "expected_explanation_shape")
        if any(not isinstance(value[field], str) or not value[field].strip() for field in text_fields):
            raise ValueError("answer quality case has empty text")
        list_fields = (
            "must_cover", "must_not_claim", "required_evidence", "known_conflicts",
            *TEACHING_FIELDS,
        )
        for field in list_fields:
            if field not in value:
                continue
            if not isinstance(value[field], list) or any(
                not isinstance(item, str) or not item.strip() for item in value[field]
            ):
                raise ValueError(f"answer quality case has invalid {field}")
        return cls(
            value["id"], value["question"], depth,
            tuple(value["must_cover"]), tuple(value["must_not_claim"]),
            tuple(value["required_evidence"]), value["expected_explanation_shape"],
            tuple(value["known_conflicts"]),
            **{field: tuple(value.get(field) or ()) for field in TEACHING_FIELDS},
        )


@dataclass(frozen=True, slots=True)
class DeterministicAnswerChecks:
    chinese_chars: int
    in_target_range: bool
    duplicate_headings: tuple[str, ...]
    repeated_conclusion_prefix: bool
    has_valid_citation: bool

    @property
    def passed(self) -> bool:
        return (
            self.in_target_range
            and not self.duplicate_headings
            and not self.repeated_conclusion_prefix
            and self.has_valid_citation
        )


@dataclass(frozen=True, slots=True)
class PairwiseJudgeResult:
    first_order_winner: str
    swapped_order_winner: str
    winner: str
    reasons: tuple[str, str]


# The judge speaks in BASELINE / CANDIDATE so the runner can compare any two
# modes without the judge knowing their names.
CANONICAL_BASELINE = "BASELINE"
CANONICAL_CANDIDATE = "CANDIDATE"
CANONICAL_TIE = "TIE"
POSITION_BIASED = "POSITION_BIASED"

_FIRST_ORDER = {"A": CANONICAL_BASELINE, "B": CANONICAL_CANDIDATE, "TIE": CANONICAL_TIE}
_SWAPPED_ORDER = {"A": CANONICAL_CANDIDATE, "B": CANONICAL_BASELINE, "TIE": CANONICAL_TIE}

JUDGE_SYSTEM_PROMPT = (
    "你是盲测评审。先判断事实正确性与证据支持，再比较直接回答、解释主线、因果链深度、"
    "心智模型清晰度、由浅入深的展开、示例是否有助于理解、失败情形的推理、取舍说明、"
    "正确性边界、连贯性、冗余与教学价值。"
    "不要因为答案更长就判它更好。"
    "不得因候选顺序偏好任何一方。只输出严格 JSON："
    '{"winner":"A|B|TIE","reason":"非空理由"}'
)


class PairwiseAnswerJudge:
    """Run the same blind comparison in both orders to expose position bias."""

    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory

    def judge(
        self, case: AnswerQualityCase, baseline_answer: str, candidate_answer: str
    ) -> PairwiseJudgeResult:
        first, first_reason = self._judge_once(case, baseline_answer, candidate_answer)
        swapped, swapped_reason = self._judge_once(case, candidate_answer, baseline_answer)
        first_canonical = _FIRST_ORDER[first]
        swapped_canonical = _SWAPPED_ORDER[swapped]
        winner = (
            first_canonical
            if first_canonical == swapped_canonical
            else POSITION_BIASED
        )
        return PairwiseJudgeResult(
            first_canonical,
            swapped_canonical,
            winner,
            (first_reason, swapped_reason),
        )

    def _judge_once(
        self, case: AnswerQualityCase, answer_a: str, answer_b: str
    ) -> tuple[str, str]:
        payload = {
            "question": case.question,
            "must_cover": list(case.must_cover),
            "must_not_claim": list(case.must_not_claim),
            "required_evidence": list(case.required_evidence),
            "expected_explanation_shape": case.expected_explanation_shape,
            "known_conflicts": list(case.known_conflicts),
            "expected_mental_model": list(case.core_mental_model),
            "must_explain_why": list(case.must_explain_why),
            "useful_scenarios": list(case.useful_scenarios),
            "misconceptions_to_correct": list(case.misconceptions),
            "pedagogy_expectations": list(case.pedagogy_expectations),
            "answer_a": answer_a,
            "answer_b": answer_b,
        }
        response = self.llm_client_factory().generate([
            LLMMessage("system", JUDGE_SYSTEM_PROMPT),
            LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
        ])
        value = json.loads(response)
        if not isinstance(value, dict) or set(value) != {"winner", "reason"}:
            raise ValueError("pairwise judge returned invalid fields")
        if value["winner"] not in {"A", "B", "TIE"}:
            raise ValueError("pairwise judge returned invalid winner")
        if not isinstance(value["reason"], str) or not value["reason"].strip():
            raise ValueError("pairwise judge returned an empty reason")
        return value["winner"], value["reason"].strip()


def load_answer_quality_cases(path: Path) -> list[AnswerQualityCase]:
    cases = [
        AnswerQualityCase.from_dict(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    identifiers = [case.id for case in cases]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("answer quality case ids must be unique")
    return cases


def check_answer_shape(
    case: AnswerQualityCase, answer: str, used_citations: list[str]
) -> DeterministicAnswerChecks:
    chinese_chars = len(re.findall(r"[\u3400-\u9fff]", answer))
    if case.answer_depth in NO_UPPER_BOUND_DEPTHS:
        in_target_range = True
    else:
        minimum, maximum = DEPTH_CHAR_RANGES[case.answer_depth]
        in_target_range = minimum <= chinese_chars <= maximum
    headings = [
        " ".join(match.split()).casefold()
        for match in re.findall(r"(?m)^#{1,6}\s+(.+)$", answer)
    ]
    duplicates = tuple(sorted({heading for heading in headings if headings.count(heading) > 1}))
    return DeterministicAnswerChecks(
        chinese_chars,
        in_target_range,
        duplicates,
        answer.count("结论：") > 1,
        bool(used_citations),
    )
