from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from devcontext.agentic.evidence_models import RequirementCoverage
from devcontext.agentic.models import error_detail as describe_error
from devcontext.context.views import CoverageView
from devcontext.llm import LLMClient, LLMMessage
from devcontext.models import ContextItem
from devcontext.observability import llm_stage, mark_last_call_wasted
from devcontext.planning import EvidenceRequirement


COVERAGE_SYSTEM_PROMPT = """你是 DevContext-Java 的证据覆盖审查器。
你只判断每条 EvidenceRequirement 名下的证据是否满足 target 与 success_criteria，不回答用户问题、不评价设计好坏、不总结最终答案。
不得使用模型记忆、常识或其他 Requirement 的证据补足项目事实。相关但不充分的证据只能判为 PARTIAL。
RELATION/PATH 需求必须同时取得符合问题语义的真实 indexed_relations 和每个端点的方法/类型正文；入口中出现调用语句不能代替目标方法正文，无关的同类边不能代替问题要求的关系。
SATISFIED 的 evidence_ids 必须包含用于证明需求的全部端点 Chunk；PATH 包含中间节点正文。只引用入口或类型摘要时不能放行方法实现/调用链。hint 只用于检索，不是证明。
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
        view: CoverageView,
    ) -> tuple[RequirementCoverage, ...]:
        """Judge each requirement against its own evidence.

        Takes a view rather than the answer's context bundle. Judging against the
        bundle meant a requirement could be reported MISSING because the answer's
        character budget had dropped its evidence, not because retrieval failed.
        Truncated items are no longer filtered out either: a coverage view is
        un-truncated by construction, so if one ever did truncate that is a bug
        worth surfacing rather than silently hiding.

        One batch LLM call still covers every requirement - only the view is
        per-requirement, not the call.
        """
        if not requirements:
            raise ValueError("coverage check requires at least one requirement")
        eligible: list[EvidenceRequirement] = []
        deterministic: dict[str, RequirementCoverage] = {}
        evidence_by_id: dict[str, list[ContextItem]] = {}
        for requirement in requirements:
            items = list(view.items_for(requirement.id))
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
                semantic = self._semantic_check(
                    eligible, evidence_by_id, getattr(view, "round_index", None), getattr(view, "workspace", None)
                )
        return tuple(
            deterministic.get(item.id) or semantic[item.id]
            for item in requirements
        )

    def _semantic_check(
        self,
        requirements: Sequence[EvidenceRequirement],
        evidence_by_id: dict[str, list[ContextItem]],
        round_index: int | None = None,
        workspace=None,
    ) -> dict[str, RequirementCoverage]:
        payload = {
            "requirements": [
                {
                    "requirement_id": requirement.id,
                    "target": requirement.target,
                    "success_criteria": requirement.success_criteria,
                    "retrieval_needs": [n.to_dict() for n in requirement.retrieval_needs],
                    "indexed_relations": [
                        {"source": r.source, "target": r.target, "edge_type": r.edge_type,
                         "source_chunk_id": r.source_chunk_id, "target_chunk_id": r.target_chunk_id}
                        for r in (workspace.relations_for(requirement.id, round_index) if workspace is not None
                                  and any(n.segments for n in requirement.retrieval_needs) else ())
                    ],
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
        error_detail = "CoverageCheckError"
        substage = None if round_index is None else f"round_{round_index}"
        for _ in range(self.max_attempts):
            try:
                client = self.llm_client_factory()  # type: ignore[misc]
                self.last_client = client
                with llm_stage("coverage_check", substage):
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
                # Retried attempts are pure discarded latency when the reply was
                # unparseable rather than the request having failed.
                mark_last_call_wasted(
                    "coverage reply rejected; retried or marked unverified",
                    stage="coverage_check",
                )
                error_detail = describe_error(exception)
        self.last_error = error_detail
        return {
            item.id: _unverified(item, evidence_by_id[item.id], error_detail)
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
            raw_ids = raw["evidence_ids"]
            if not isinstance(raw_ids, list) or any(
                not isinstance(item, int) for item in raw_ids
            ):
                raise CoverageCheckError("coverage evidence ids must be integers")
            # An id that is not this requirement's evidence cannot support it, so
            # it is dropped rather than failing the batch. Rejecting the batch
            # over one bad id turned a single slip into six UNVERIFIED
            # requirements, which reaches the user as six missing pieces of
            # evidence - a much stronger claim than "the checker misfired here".
            evidence_ids = [item for item in raw_ids if item in allowed_ids]
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
