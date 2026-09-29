from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from devcontext.answer.generator import extract_citations
from devcontext.answer.models import (
    REVIEW_ISSUE_TYPES,
    AnswerPlan,
    GroundedDraft,
    ReviewIssue,
    ReviewResult,
)
from devcontext.llm import LLMClient, LLMMessage
from devcontext.models import ContextBundle
from devcontext.planning import QuestionPlan
from devcontext.request import UserRequest

if TYPE_CHECKING:
    from devcontext.agentic.evidence_models import EvidencePackage


REVIEW_SYSTEM_PROMPT = """你是 DevContext-Java 的回答审稿器。检查事实支持、来源冲突、核心遗漏、重复、机械模板、顺序和过度推断。
只能保留 Context 中真实存在的 Citation；不得加入 Context 没有的项目事实。最多修订一次。
当前实现优先依据 IMPLEMENTATION/CURRENT；验证报告只证明明确记录的结果；设计、历史和未来文档不得冒充当前代码事实。
必须检查并执行 Answer Plan 的篇幅：brief 150–500、standard 800–1800、detailed 2200–5000 中文字符；不在范围内时标记 LENGTH_VIOLATION，并在不重复、不虚构的前提下修订到范围内。
issue_type 只能是 UNSUPPORTED_CLAIM、SOURCE_CONFLICT、MISSING_CORE_POINT、REPETITION、MECHANICAL_STRUCTURE、POOR_ORDER、OVERCLAIM 或 LENGTH_VIOLATION。
只输出严格 JSON：accepted、issues、final_answer_with_citations。issues 每项只有 issue_type 和 description。"""


class AnswerReviewer:
    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory
        self.last_client: LLMClient | None = None

    def review(
        self,
        query: str,
        question_plan: QuestionPlan,
        answer_plan: AnswerPlan,
        draft: GroundedDraft,
        context: ContextBundle,
    ) -> ReviewResult:
        payload = {
            "question": query,
            "investigation_plan": question_plan.to_dict(),
            "answer_plan": answer_plan.to_dict(),
            "draft": draft.text_with_citations,
            "context": context.rendered_text,
        }
        self.last_client = self.llm_client_factory()
        response = self.last_client.generate([
            LLMMessage("system", REVIEW_SYSTEM_PROMPT),
            LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
        ])
        return self._parse(response, context)

    def review_evidence(
        self,
        request: UserRequest,
        evidence_package: "EvidencePackage",
        answer_plan: AnswerPlan,
        draft: GroundedDraft,
    ) -> ReviewResult:
        payload = {
            "question": request.original_query,
            "answer_options": request.answer_options.to_dict(),
            "evidence_plan": evidence_package.evidence_plan.to_dict(),
            "requirement_coverage": [
                item.to_dict() for item in evidence_package.requirement_coverage
            ],
            "retrieval_state": evidence_package.retrieval_state,
            "answer_plan": answer_plan.to_dict(),
            "draft": draft.text_with_citations,
            "context": evidence_package.context_bundle.rendered_text,
        }
        self.last_client = self.llm_client_factory()
        response = self.last_client.generate([
            LLMMessage("system", REVIEW_SYSTEM_PROMPT),
            LLMMessage("user", json.dumps(payload, ensure_ascii=False)),
        ])
        return self._parse(response, evidence_package.context_bundle)

    @staticmethod
    def _parse(response: str, context: ContextBundle) -> ReviewResult:
        value = json.loads(response)
        if not isinstance(value, dict) or set(value) != {
            "accepted", "issues", "final_answer_with_citations"
        }:
            raise ValueError("review has invalid fields")
        if not isinstance(value["accepted"], bool) or not isinstance(value["issues"], list):
            raise ValueError("review has invalid values")
        issues: list[ReviewIssue] = []
        for raw in value["issues"]:
            if not isinstance(raw, dict) or set(raw) != {"issue_type", "description"}:
                raise ValueError("review issue has invalid fields")
            if raw["issue_type"] not in REVIEW_ISSUE_TYPES:
                raise ValueError("review issue type is invalid")
            issues.append(ReviewIssue(raw["issue_type"], str(raw["description"])))
        answer = value["final_answer_with_citations"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("review answer is empty")
        allowed = {item.citation.label for item in context.items}
        labels = extract_citations(answer)
        if not labels:
            raise ValueError("review answer contains no citation")
        if any(label not in allowed for label in labels):
            raise ValueError("review answer contains an invalid citation")
        return ReviewResult(value["accepted"], tuple(issues), answer.strip())
