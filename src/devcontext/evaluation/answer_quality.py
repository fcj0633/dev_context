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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AnswerQualityCase":
        required = {
            "id", "question", "answer_depth", "must_cover", "must_not_claim",
            "required_evidence", "expected_explanation_shape", "known_conflicts",
        }
        if set(value) != required:
            raise ValueError("answer quality case has invalid fields")
        depth = value["answer_depth"]
        if depth not in DEPTH_CHAR_RANGES:
            raise ValueError("answer quality case has invalid depth")
        text_fields = ("id", "question", "expected_explanation_shape")
        if any(not isinstance(value[field], str) or not value[field].strip() for field in text_fields):
            raise ValueError("answer quality case has empty text")
        list_fields = ("must_cover", "must_not_claim", "required_evidence", "known_conflicts")
        for field in list_fields:
            if not isinstance(value[field], list) or any(
                not isinstance(item, str) or not item.strip() for item in value[field]
            ):
                raise ValueError(f"answer quality case has invalid {field}")
        return cls(
            value["id"], value["question"], depth,
            tuple(value["must_cover"]), tuple(value["must_not_claim"]),
            tuple(value["required_evidence"]), value["expected_explanation_shape"],
            tuple(value["known_conflicts"]),
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


class PairwiseAnswerJudge:
    """Run the same blind comparison in both orders to expose position bias."""

    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory

    def judge(
        self, case: AnswerQualityCase, v1_answer: str, v2_answer: str
    ) -> PairwiseJudgeResult:
        first, first_reason = self._judge_once(case, v1_answer, v2_answer)
        swapped, swapped_reason = self._judge_once(case, v2_answer, v1_answer)
        first_canonical = {"A": "V1", "B": "V2", "TIE": "TIE"}[first]
        swapped_canonical = {"A": "V2", "B": "V1", "TIE": "TIE"}[swapped]
        winner = (
            first_canonical
            if first_canonical == swapped_canonical
            else "POSITION_BIASED"
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
            "answer_a": answer_a,
            "answer_b": answer_b,
        }
        response = self.llm_client_factory().generate([
            LLMMessage(
                "system",
                "你是盲测评审。比较当前事实正确性、证据支持、直接回答、解释主线、因果边界、覆盖、重复、不确定性和可读性。"
                "不得因候选顺序偏好任何一方。只输出严格 JSON："
                '{"winner":"A|B|TIE","reason":"非空理由"}',
            ),
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
    minimum, maximum = DEPTH_CHAR_RANGES[case.answer_depth]
    headings = [
        " ".join(match.split()).casefold()
        for match in re.findall(r"(?m)^#{1,6}\s+(.+)$", answer)
    ]
    duplicates = tuple(sorted({heading for heading in headings if headings.count(heading) > 1}))
    return DeterministicAnswerChecks(
        chinese_chars,
        minimum <= chinese_chars <= maximum,
        duplicates,
        answer.count("结论：") > 1,
        bool(used_citations),
    )
