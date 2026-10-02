from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from devcontext.answer.generator import EVIDENCE_CITATION_PATTERN, extract_citations
from devcontext.explanation.models import DraftSection, ExplanationPlan
from devcontext.explanation.writer import TeachingDraft
from devcontext.llm import LLMClient, LLMMessage
from devcontext.observability import llm_stage, mark_last_call_wasted


COMPOSER_MAX_TOKENS = 16_384

COMPOSER_SYSTEM_PROMPT = """你是 DevContext-Java 的解释编辑。你拿到的是已经写好的若干章节草稿，把它们组织成一篇连贯的文章。

允许：调整章节顺序、补写过渡句、删除重复、统一术语、改善开头与结尾。
禁止：新增任何项目事实、新增任何 Citation 标记、提到草稿中未出现过的类名或符号。
你不得引入新的 [E数字] 标记——可用的标记只有草稿里已经出现过的那些。
保持每节原有的 Citation 归属：不要把某节的证据挪去支撑另一节的结论。
只输出编排后的正文，不要输出解释、不要输出 JSON、不要输出 Sources 列表。"""

COMPOSER_PROMPT = """编排要求：
1. 保留每节的小标题，形如 "## 标题"。
2. 章节顺序可在有助于理解时调整，但每一节的论据必须留在它自己的节内。
3. 首段直接回答用户的问题。
4. 只输出正文。"""


class SectionComposer:
    """Joins section drafts into one answer.

    This is the only stage that sees the whole argument at once, so it is where
    ordering and transitions are decided - and where a careless model is most
    tempted to add a fact no section supported.
    """

    def __init__(self, llm_client_factory: Callable[[], LLMClient]) -> None:
        self.llm_client_factory = llm_client_factory
        self.last_client: LLMClient | None = None
        # Composing runs twice on the revision path, so the call number is what
        # tells the two apart in the trace.
        self.compose_count = 0

    def compose(
        self,
        query: str,
        plan: ExplanationPlan,
        drafts: Sequence[DraftSection],
    ) -> TeachingDraft:
        if not drafts:
            return TeachingDraft("", (), ())

        permitted = {
            label for draft in drafts for label in draft.used_citations
        }
        payload = {
            "question": query,
            "core_mental_model": plan.core_mental_model,
            "answer_depth": plan.answer_depth,
            "sections": [
                {
                    "id": draft.section_id,
                    "title": draft.title,
                    "text": draft.text_with_citations,
                }
                for draft in drafts
            ],
        }
        self.compose_count += 1
        self.last_client = self.llm_client_factory()
        try:
            with llm_stage("composer", f"call_{self.compose_count}"):
                response = self.last_client.generate([
                    LLMMessage("system", COMPOSER_SYSTEM_PROMPT),
                    LLMMessage(
                        "user",
                        json.dumps(payload, ensure_ascii=False) + "\n\n" + COMPOSER_PROMPT,
                    ),
                ]).strip()
        except Exception:
            mark_last_call_wasted("composer call failed; deterministic join", stage="composer")
            return deterministic_join(drafts)

        labels = extract_citations(response, EVIDENCE_CITATION_PATTERN)
        introduced = [label for label in labels if label not in permitted]
        if introduced or not response:
            # A composer that invents evidence has broken the one rule it cannot
            # be trusted to keep, so its output is discarded rather than patched.
            mark_last_call_wasted(
                "composer output discarded; deterministic join", stage="composer"
            )
            return deterministic_join(drafts)
        return TeachingDraft(
            text_with_citations=response,
            used_citations=tuple(dict.fromkeys(labels)),
            invalid_citations=(),
        )


def deterministic_join(drafts: Sequence[DraftSection]) -> TeachingDraft:
    parts = []
    labels: list[str] = []
    for draft in drafts:
        parts.append(f"## {draft.title}\n\n{draft.text_with_citations}")
        labels.extend(draft.used_citations)
    return TeachingDraft(
        text_with_citations="\n\n".join(parts),
        used_citations=tuple(dict.fromkeys(labels)),
        invalid_citations=(),
    )


# Kept for callers/tests that imported the old private helper.
_deterministic_join = deterministic_join
