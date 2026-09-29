from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from devcontext.agentic.evidence_models import RequirementCoverage
from devcontext.llm import LLMClient, LLMMessage
from devcontext.models import ContextBundle, ContextItem
from devcontext.planning import EvidenceRequirement


COVERAGE_SYSTEM_PROMPT = """你是 DevContext-Java 的证据覆盖审查器。
你只判断每条 EvidenceRequirement 名下的证据是否满足 target 与 success_criteria，不回答用户问题、不评价设计好坏、不总结最终答案。
不得使用模型记忆、常识或其他 Requirement 的证据补足项目事实。相关但不充分的证据只能判为 PARTIAL。
state 只能是 SATISFIED、PARTIAL 或 MISSING。reason 必须简短；missing_criteria 最多三条，每条只描述尚缺的证据内容。
statuses 必须与输入 requirements 数量和顺序完全一致。
只输出严格 JSON：{"statuses": [{"requirement_id": "ER1", "state": "PARTIAL", "evidence_ids": [1], "missing_criteria": ["尚缺内容"], "reason": "简短理由"}]}。"""

MAX_REASON_CHARS = 160
MAX_MISSING_CRITERIA = 3


class CoverageCheckError(RuntimeError):
    pass


class CoverageChecker:
    def __init__(
        self,
        llm_client_factory: Callable[[], LLMClient] | None = None,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts not in {1, 2}:
            raise ValueError("max_attempts must be 1 or 2")
        self.llm_client_factory = llm_client_factory
        self.max_attempts = max_attempts
        self.last_client: LLMClient | None = None
        self.last_error: str | None = None

    def check(
        self,
        requirements: Sequence[EvidenceRequirement],
        context: ContextBundle,
    ) -> tuple[RequirementCoverage, ...]:
        if not requirements:
            raise ValueError("coverage check requires at least one requirement")
        eligible: list[EvidenceRequirement] = []
        deterministic: dict[str, RequirementCoverage] = {}
        evidence_by_id: dict[str, list[ContextItem]] = {}
        for requirement in requirements:
            items = [
                item
                for item in context.items
                if requirement.id in item.sub_question_ids and not item.truncated
            ]
            evidence_by_id[requirement.id] = items
            source_gap = _missing_sources(requirement, items)
            if source_gap:
                state = "PARTIAL" if items else "MISSING"
                deterministic[requirement.id] = RequirementCoverage(
                    requirement.id,
                    state,
                    tuple(item.chunk_id for item in items),
                    tuple(source_gap[:MAX_MISSING_CRITERIA]),
                    "所需证据来源尚未完整进入最终 Context",
                    "rules",
                )
            else:
                eligible.append(requirement)

        semantic: dict[str, RequirementCoverage] = {}
        if eligible:
            if self.llm_client_factory is None:
                semantic = {
                    item.id: _unverified(item, evidence_by_id[item.id], "checker unavailable")
                    for item in eligible
                }
            else:
                semantic = self._semantic_check(eligible, evidence_by_id)
        return tuple(
            deterministic.get(item.id) or semantic[item.id]
            for item in requirements
        )

    def _semantic_check(
        self,
        requirements: Sequence[EvidenceRequirement],
        evidence_by_id: dict[str, list[ContextItem]],
    ) -> dict[str, RequirementCoverage]:
        payload = {
            "requirements": [
                {
                    "requirement_id": requirement.id,
                    "target": requirement.target,
                    "success_criteria": requirement.success_criteria,
                    "evidence": [
                        {
                            "chunk_id": item.chunk_id,
                            "source_type": item.citation.source_type,
                            "file_path": item.citation.file_path,
                            "identity": (
                                item.citation.signature
                                or item.citation.symbol_name
                                or item.citation.class_name
                                or " > ".join(item.citation.heading_path)
                            ),
                            "content": item.content,
                        }
                        for item in evidence_by_id[requirement.id]
                    ],
                }
                for requirement in requirements
            ]
        }
        error_name = "CoverageCheckError"
        for _ in range(self.max_attempts):
            try:
                client = self.llm_client_factory()  # type: ignore[misc]
                self.last_client = client
                response = client.generate(
                    [
                        LLMMessage("system", COVERAGE_SYSTEM_PROMPT),
                        LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
                    ]
                )
                parsed = self._parse(response, requirements, evidence_by_id)
                self.last_error = None
                return {item.requirement_id: item for item in parsed}
            except Exception as exception:
                error_name = type(exception).__name__
        self.last_error = error_name
        return {
            item.id: _unverified(item, evidence_by_id[item.id], error_name)
            for item in requirements
        }

    @staticmethod
    def _parse(
        response: str,
        requirements: Sequence[EvidenceRequirement],
        evidence_by_id: dict[str, list[ContextItem]],
    ) -> tuple[RequirementCoverage, ...]:
        try:
            value = json.loads(response)
        except json.JSONDecodeError as exception:
            raise CoverageCheckError("coverage output is not valid JSON") from exception
        if not isinstance(value, dict) or set(value) != {"statuses"}:
            raise CoverageCheckError("coverage output has invalid fields")
        raw_statuses = value["statuses"]
        if not isinstance(raw_statuses, list) or len(raw_statuses) != len(requirements):
            raise CoverageCheckError("coverage status count is invalid")
        statuses: list[RequirementCoverage] = []
        for raw, requirement in zip(raw_statuses, requirements, strict=True):
            if not isinstance(raw, dict) or set(raw) != {
                "requirement_id",
                "state",
                "evidence_ids",
                "missing_criteria",
                "reason",
            }:
                raise CoverageCheckError("coverage status has invalid fields")
            if raw["requirement_id"] != requirement.id:
                raise CoverageCheckError("coverage status order or id is invalid")
            if raw["state"] not in {"SATISFIED", "PARTIAL", "MISSING"}:
                raise CoverageCheckError("coverage state is invalid")
            allowed_ids = {item.chunk_id for item in evidence_by_id[requirement.id]}
            evidence_ids = raw["evidence_ids"]
            if (
                not isinstance(evidence_ids, list)
                or any(not isinstance(item, int) or item not in allowed_ids for item in evidence_ids)
            ):
                raise CoverageCheckError("coverage evidence ids are invalid")
            raw_missing = raw["missing_criteria"]
            if not isinstance(raw_missing, list) or len(raw_missing) > MAX_MISSING_CRITERIA:
                raise CoverageCheckError("missing criteria are invalid")
            missing = tuple(
                _short_text(item, "missing_criteria") for item in raw_missing
            )
            reason = _short_text(raw["reason"], "reason")
            if raw["state"] == "SATISFIED" and missing:
                raise CoverageCheckError("satisfied coverage cannot have missing criteria")
            if raw["state"] == "SATISFIED" and not evidence_ids:
                raise CoverageCheckError("satisfied coverage requires direct evidence")
            if raw["state"] != "SATISFIED" and not missing:
                raise CoverageCheckError("incomplete coverage requires missing criteria")
            statuses.append(
                RequirementCoverage(
                    requirement.id,
                    raw["state"],
                    tuple(evidence_ids),
                    missing,
                    reason,
                    "llm",
                )
            )
        return tuple(statuses)


def _missing_sources(
    requirement: EvidenceRequirement,
    items: Sequence[ContextItem],
) -> list[str]:
    present = {item.citation.source_type for item in items}
    required = requirement.source_requirement
    if required == "ANY":
        return [] if present & {"CODE", "DOCUMENT"} else ["缺少直接相关的项目证据"]
    if required == "CODE":
        return [] if "CODE" in present else ["缺少直接相关的 CODE 证据"]
    if required == "DOCUMENT":
        return [] if "DOCUMENT" in present else ["缺少直接相关的 DOCUMENT 证据"]
    missing: list[str] = []
    if "CODE" not in present:
        missing.append("缺少直接相关的 CODE 证据")
    if "DOCUMENT" not in present:
        missing.append("缺少直接相关的 DOCUMENT 证据")
    return missing


def _unverified(
    requirement: EvidenceRequirement,
    items: Sequence[ContextItem],
    error_name: str,
) -> RequirementCoverage:
    return RequirementCoverage(
        requirement.id,
        "UNVERIFIED",
        tuple(item.chunk_id for item in items),
        (requirement.success_criteria,),
        "已有候选证据，但语义覆盖检查未能完成",
        "fallback",
        error_name,
    )


def _short_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise CoverageCheckError(f"{field} must be text")
    text = " ".join(value.strip().split())
    if not text or len(text) > MAX_REASON_CHARS or "\n" in value or "\r" in value:
        raise CoverageCheckError(f"{field} is invalid")
    return text
