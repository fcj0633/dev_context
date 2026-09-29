from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from devcontext.models import SearchResult
from devcontext.planning import EvidenceRequirement, SubQuestion


SOURCE_ROLES = (
    "IMPLEMENTATION",
    "VERIFICATION",
    "CURRENT_DESIGN",
    "GENERAL_DOCUMENT",
    "HISTORICAL_PLAN",
    "UNKNOWN",
)
TEMPORAL_STATUSES = ("CURRENT", "HISTORICAL", "FUTURE", "UNKNOWN")


@dataclass(frozen=True, slots=True)
class SourceRule:
    pattern: str
    source_role: str
    temporal_status: str
    priority: int


@dataclass(frozen=True, slots=True)
class EvidenceAnnotation:
    source_role: str
    temporal_status: str
    authority_priority: int
    sub_question_ids: tuple[str, ...] = ()

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        return self.sub_question_ids


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    sub_question_id: str
    search_result: SearchResult
    source_role: str
    temporal_status: str
    authority_priority: int

    @property
    def requirement_id(self) -> str:
        return self.sub_question_id


@dataclass(slots=True)
class EvidencePool:
    candidates_by_sub_question: dict[str, list[EvidenceCandidate]] = field(
        default_factory=dict
    )

    def add(self, candidate: EvidenceCandidate) -> None:
        candidates = self.candidates_by_sub_question.setdefault(
            candidate.sub_question_id, []
        )
        if all(item.search_result.id != candidate.search_result.id for item in candidates):
            candidates.append(candidate)

    def add_many(self, candidates: Iterable[EvidenceCandidate]) -> None:
        for candidate in candidates:
            self.add(candidate)

    def select(
        self,
        sub_questions: Iterable[SubQuestion | EvidenceRequirement],
        max_results: int,
    ) -> tuple[list[SearchResult], dict[int, EvidenceAnnotation]]:
        """Select a fair, authority-aware context without losing attribution."""
        if max_results < 1:
            return [], {}
        questions = list(sub_questions)
        questions_by_id = {item.id: item for item in questions}
        selected: list[EvidenceCandidate] = []
        selected_ids: set[int] = set()

        def choose(
            question: SubQuestion | EvidenceRequirement,
            source_type: str | None = None,
        ) -> None:
            if len(selected) >= max_results:
                return
            candidates = self.candidates_by_sub_question.get(question.id, [])
            ordered = [
                item
                for _, item in sorted(
                    enumerate(candidates),
                    key=lambda pair: (
                        -_effective_priority(pair[1], question),
                        pair[0],
                    ),
                )
            ]
            for candidate in ordered:
                result = candidate.search_result
                if result.id in selected_ids:
                    continue
                if result.source_type not in _owner_sources(question):
                    continue
                if source_type and result.source_type != source_type:
                    continue
                selected.append(candidate)
                selected_ids.add(result.id)
                return

        core = [item for item in questions if _owner_priority(item) == "CORE"]
        supporting = [item for item in questions if _owner_priority(item) != "CORE"]
        for question in core:
            choose(question)
        for question in core:
            if set(_owner_sources(question)) == {"CODE", "DOCUMENT"}:
                present = {
                    item.search_result.source_type
                    for item in selected
                    if item.sub_question_id == question.id
                }
                for source_type in ("CODE", "DOCUMENT"):
                    if source_type not in present:
                        choose(question, source_type)
        for question in supporting:
            choose(question)

        remaining = [
            (item, rank)
            for candidates in self.candidates_by_sub_question.values()
            for rank, item in enumerate(candidates)
            if item.search_result.id not in selected_ids
        ]
        for candidate, _ in sorted(
            remaining,
            key=lambda pair: (
                -_effective_priority(
                    pair[0], questions_by_id.get(pair[0].sub_question_id)
                ),
                pair[1],
            ),
        ):
            if len(selected) >= max_results:
                break
            if candidate.search_result.id in selected_ids:
                continue
            owner = questions_by_id.get(candidate.sub_question_id)
            if owner and candidate.search_result.source_type not in _owner_sources(owner):
                continue
            selected.append(candidate)
            selected_ids.add(candidate.search_result.id)

        annotations: dict[int, EvidenceAnnotation] = {}
        for candidate in selected:
            chunk_id = candidate.search_result.id
            owners = tuple(
                question_id
                for question_id, candidates in self.candidates_by_sub_question.items()
                if any(item.search_result.id == chunk_id for item in candidates)
            )
            annotations[chunk_id] = EvidenceAnnotation(
                candidate.source_role,
                candidate.temporal_status,
                candidate.authority_priority,
                owners,
            )
        return [item.search_result for item in selected], annotations


class SourcePolicy:
    def __init__(self, rules: Iterable[SourceRule] = ()) -> None:
        self.rules = tuple(rules)

    @classmethod
    def from_file(cls, path: Path | None) -> "SourcePolicy":
        if path is None or not path.is_file():
            return cls()
        value = json.loads(path.read_text(encoding="utf-8"))
        raw_rules = value.get("rules") if isinstance(value, dict) else None
        if not isinstance(raw_rules, list):
            raise ValueError("source policy must contain a rules list")
        rules: list[SourceRule] = []
        for raw in raw_rules:
            if not isinstance(raw, dict) or set(raw) != {
                "pattern", "source_role", "temporal_status", "priority"
            }:
                raise ValueError("source policy rule has invalid fields")
            role = raw["source_role"]
            status = raw["temporal_status"]
            priority = raw["priority"]
            if not isinstance(raw["pattern"], str) or not raw["pattern"].strip():
                raise ValueError("source policy pattern must be non-empty text")
            if role not in SOURCE_ROLES or status not in TEMPORAL_STATUSES:
                raise ValueError("source policy rule has invalid role or status")
            if not isinstance(priority, int) or not 0 <= priority <= 100:
                raise ValueError("source policy priority must be between 0 and 100")
            rules.append(SourceRule(raw["pattern"], role, status, priority))
        return cls(rules)

    def classify(
        self, result: SearchResult, sub_question_id: str
    ) -> EvidenceCandidate:
        if result.source_type == "CODE":
            return EvidenceCandidate(
                sub_question_id, result, "IMPLEMENTATION", "CURRENT", 100
            )
        normalized = result.file_path.replace("\\", "/")
        for rule in self.rules:
            pattern = rule.pattern.casefold()
            path = normalized.casefold()
            if fnmatch.fnmatch(path, pattern) or (
                pattern.startswith("**/")
                and fnmatch.fnmatch(path, pattern[3:])
            ):
                return EvidenceCandidate(
                    sub_question_id,
                    result,
                    rule.source_role,
                    rule.temporal_status,
                    rule.priority,
                )
        return EvidenceCandidate(
            sub_question_id, result, "UNKNOWN", "UNKNOWN", 50
        )


def default_source_policy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "source-policy.json"


def _effective_priority(
    candidate: EvidenceCandidate,
    question: SubQuestion | EvidenceRequirement | None,
) -> int:
    if question is None or question.temporal_scope == "ANY":
        return candidate.authority_priority
    preferred_status = {
        "CURRENT": "CURRENT",
        "HISTORY": "HISTORICAL",
        "FUTURE": "FUTURE",
    }.get(question.temporal_scope)
    if candidate.temporal_status == preferred_status:
        return candidate.authority_priority + 25
    if (
        question.temporal_scope == "CURRENT"
        and candidate.temporal_status in {"HISTORICAL", "FUTURE"}
    ):
        return candidate.authority_priority - 25
    return candidate.authority_priority


def _owner_priority(owner: SubQuestion | EvidenceRequirement) -> str:
    return getattr(owner, "priority", getattr(owner, "importance", "CORE"))


def _owner_sources(
    owner: SubQuestion | EvidenceRequirement,
) -> tuple[str, ...]:
    source_requirement = getattr(owner, "source_requirement", None)
    if source_requirement == "CODE":
        return ("CODE",)
    if source_requirement == "DOCUMENT":
        return ("DOCUMENT",)
    if source_requirement in {"BOTH", "ANY"}:
        return ("CODE", "DOCUMENT")
    return tuple(getattr(owner, "preferred_sources", ("CODE", "DOCUMENT")))
