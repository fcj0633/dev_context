from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence

from devcontext.agentic.evidence_models import RequirementCoverage, SearchAction
from devcontext.llm import LLMClient, LLMMessage
from devcontext.planning import EvidenceRequirement


SEARCH_ACTION_SYSTEM_PROMPT = """你是 DevContext-Java 检索控制器内部的查询生成器。
你只为给定 EvidenceRequirement 生成本轮实际搜索文本，不回答用户问题、不创建新需求、不改变需求来源和优先级。
首次搜索应结合原问题、target 和 success_criteria，表达当前项目中要查找的实现或文档证据。
后续搜索必须针对 missing_criteria，并优先复用上一轮真实证据中已发现的类名、方法名、文件名、标题和业务术语。
不得虚构项目符号；不得输出多个候选 Query；不得重复历史 Query。
只输出严格 JSON：{"actions": [{"requirement_id": "ER1", "query": "单一查询文本", "reason": "简短原因"}]}。"""

MAX_QUERY_CHARS = 500
MAX_REASON_CHARS = 160


class SearchActionPlanError(RuntimeError):
    pass


class SearchActionPlanner:
    def __init__(
        self,
        llm_client_factory: Callable[[], LLMClient] | None = None,
    ) -> None:
        self.llm_client_factory = llm_client_factory
        self.last_client: LLMClient | None = None

    def plan_actions(
        self,
        original_query: str,
        requirements: Sequence[EvidenceRequirement],
        *,
        round_index: int,
        history: Sequence[SearchAction] = (),
        coverage: Mapping[str, RequirementCoverage] | None = None,
        discovered_terms: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[SearchAction, ...]:
        if not original_query.strip():
            raise ValueError("original_query must not be empty")
        if not requirements:
            return ()
        coverage = coverage or {}
        discovered_terms = discovered_terms or {}
        history_by_id: dict[str, list[str]] = {}
        for action in history:
            history_by_id.setdefault(action.requirement_id, []).append(action.query)
        if self.llm_client_factory is None:
            return _fallback_actions(
                original_query,
                requirements,
                round_index,
                history,
                coverage,
                discovered_terms,
            )
        payload = {
            "original_query": original_query,
            "round_index": round_index,
            "requirements": [
                {
                    "requirement_id": item.id,
                    "target": item.target,
                    "success_criteria": item.success_criteria,
                    "source_requirement": item.source_requirement,
                    "missing_criteria": list(
                        coverage[item.id].missing_criteria
                        if item.id in coverage else ()
                    ),
                    "discovered_terms": list(discovered_terms.get(item.id, ()))[:8],
                    "previous_queries": history_by_id.get(item.id, []),
                }
                for item in requirements
            ],
        }
        try:
            client = self.llm_client_factory()
            self.last_client = client
            response = client.generate(
                [
                    LLMMessage("system", SEARCH_ACTION_SYSTEM_PROMPT),
                    LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
                ]
            )
            return self._parse(
                response,
                requirements,
                round_index,
                history,
                coverage,
                discovered_terms,
            )
        except Exception:
            return _fallback_actions(
                original_query,
                requirements,
                round_index,
                history,
                coverage,
                discovered_terms,
            )

    @staticmethod
    def _parse(
        response: str,
        requirements: Sequence[EvidenceRequirement],
        round_index: int,
        history: Sequence[SearchAction],
        coverage: Mapping[str, RequirementCoverage],
        discovered_terms: Mapping[str, Sequence[str]],
    ) -> tuple[SearchAction, ...]:
        try:
            value = json.loads(response)
        except json.JSONDecodeError as exception:
            raise SearchActionPlanError("search action plan is not valid JSON") from exception
        if not isinstance(value, dict) or set(value) != {"actions"}:
            raise SearchActionPlanError("search action plan has invalid fields")
        raw_actions = value["actions"]
        if not isinstance(raw_actions, list) or len(raw_actions) != len(requirements):
            raise SearchActionPlanError("search action count does not match requirements")
        expected = [item.id for item in requirements]
        previous = {
            _normalize(item.query).casefold() for item in history
        }
        actions: list[SearchAction] = []
        for offset, (raw, requirement) in enumerate(
            zip(raw_actions, requirements, strict=True), start=1
        ):
            if not isinstance(raw, dict) or set(raw) != {
                "requirement_id", "query", "reason"
            }:
                raise SearchActionPlanError("search action has invalid fields")
            if raw["requirement_id"] != requirement.id:
                raise SearchActionPlanError("search action order or id is invalid")
            query = _text(raw["query"], "query", MAX_QUERY_CHARS)
            reason = _text(raw["reason"], "reason", MAX_REASON_CHARS)
            if query.casefold() in previous:
                raise SearchActionPlanError("search action repeats a previous query")
            if round_index > 0:
                terms = [
                    _normalize(item).casefold()
                    for item in discovered_terms.get(requirement.id, ())
                    if _normalize(item)
                ]
                if terms and not any(term in query.casefold() for term in terms):
                    raise SearchActionPlanError(
                        "follow-up action does not use a discovered project term"
                    )
                missing = [
                    _normalize(item).casefold()
                    for item in coverage.get(
                        requirement.id,
                        RequirementCoverage(
                            requirement.id,
                            "MISSING",
                            (),
                            (),
                            "",
                            "rules",
                        ),
                    ).missing_criteria
                    if _normalize(item)
                ]
                if not terms and missing and not any(
                    item in query.casefold() for item in missing
                ):
                    raise SearchActionPlanError(
                        "follow-up action does not target a missing criterion"
                    )
            previous.add(query.casefold())
            actions.append(
                SearchAction(
                    f"SA{len(history) + offset}",
                    requirement.id,
                    round_index,
                    query,
                    requirement.source_requirement,
                    reason,
                    "llm",
                )
            )
        if [item.requirement_id for item in actions] != expected:
            raise SearchActionPlanError("search action requirements are incomplete")
        return tuple(actions)


def _fallback_actions(
    original_query: str,
    requirements: Sequence[EvidenceRequirement],
    round_index: int,
    history: Sequence[SearchAction],
    coverage: Mapping[str, RequirementCoverage],
    discovered_terms: Mapping[str, Sequence[str]],
) -> tuple[SearchAction, ...]:
    previous_by_id: dict[str, set[str]] = {}
    for action in history:
        previous_by_id.setdefault(action.requirement_id, set()).add(
            _normalize(action.query).casefold()
        )
    actions: list[SearchAction] = []
    for offset, requirement in enumerate(requirements, start=1):
        missing = " ".join(
            coverage.get(
                requirement.id,
                RequirementCoverage(
                    requirement.id,
                    "MISSING",
                    (),
                    (requirement.success_criteria,),
                    "尚未搜索",
                    "rules",
                ),
            ).missing_criteria
        )
        terms = " ".join(discovered_terms.get(requirement.id, ())[:8])
        query = _normalize(
            f"{original_query} {requirement.target} {missing} {terms}"
        )[:MAX_QUERY_CHARS]
        if query.casefold() in previous_by_id.get(requirement.id, set()):
            query = _normalize(
                f"{requirement.target} {requirement.success_criteria} {terms}"
            )[:MAX_QUERY_CHARS]
        if query.casefold() in previous_by_id.get(requirement.id, set()):
            query = _normalize(f"{query} 补充证据")[:MAX_QUERY_CHARS]
        actions.append(
            SearchAction(
                f"SA{len(history) + offset}",
                requirement.id,
                round_index,
                query,
                requirement.source_requirement,
                "按证据目标和当前缺口生成确定性查询",
                "fallback",
            )
        )
    return tuple(actions)


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SearchActionPlanError(f"{field} must be text")
    text = _normalize(value)
    if not text or "```" in value or "\n" in value or "\r" in value:
        raise SearchActionPlanError(f"{field} must be single-line plain text")
    if len(text) > maximum:
        raise SearchActionPlanError(f"{field} exceeds the length limit")
    return text


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
