from __future__ import annotations

import pytest

from devcontext.answer import (
    EMPTY_CONTEXT_ANSWER,
    AnswerGenerator,
    describe_sections,
    extract_citations,
    format_source,
    strip_citations,
)
from devcontext.llm import LLMMessage
from devcontext.models import Citation, ContextBundle, ContextItem


class FakeLLMClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[list[LLMMessage]] = []

    def generate(self, messages: list[LLMMessage]) -> str:
        self.calls.append(list(messages))
        return self.answer


def bundle(*labels: str) -> ContextBundle:
    items = [
        ContextItem(
            citation=Citation(
                label=label,
                source_type="CODE" if index == 1 else "DOCUMENT",
                file_path="Service.java" if index == 1 else "design.md",
                class_name="Service" if index == 1 else None,
                symbol_name="run" if index == 1 else None,
                start_line=10 if index == 1 else None,
                end_line=20 if index == 1 else None,
                heading_path=[] if index == 1 else ["设计", "事务"],
            ),
            content=f"evidence {label}",
            chunk_id=index,
            chunk_type="METHOD" if index == 1 else "DOCUMENT_SECTION",
            score=0.5,
            retrieval_rank=index,
        )
        for index, label in enumerate(labels, start=1)
    ]
    rendered = "\n\n".join(
        f"[{item.citation.label}] {item.content}" for item in items
    )
    return ContextBundle(
        query="原始问题",
        items=items,
        rendered_text=rendered,
        total_chars=len(rendered),
        max_chars=6000,
        truncated=False,
    )


def test_generator_passes_query_and_context_to_grounded_prompt() -> None:
    context_bundle = bundle("C1", "C2")
    client = FakeLLMClient("事务入口见 Service#run。\n[C1][C2]")

    result = AnswerGenerator(client).generate("购票事务如何实现？", context_bundle)

    assert result.answer == "事务入口见 Service#run。"
    assert result.used_citations == ["C1", "C2"]
    assert result.invalid_citations == []
    assert result.zero_valid_citation is False
    assert len(client.calls) == 1
    messages = client.calls[0]
    assert [message.role for message in messages] == ["system", "user"]
    assert "只能依据" in messages[0].content
    assert "不得使用模型记忆" in messages[0].content
    assert "购票事务如何实现？" in messages[1].content
    assert context_bundle.rendered_text in messages[1].content
    assert "[C1], [C2]" in messages[1].content


def test_outline_adds_answer_chain_and_section_budget() -> None:
    client = FakeLLMClient("第一节内容。\n[C1]")

    AnswerGenerator(client).generate(
        "问题",
        bundle("C1"),
        outline="Answer Outline:\nIntent: 了解流程\nDepth: detailed\n1. 入口在哪",
    )

    prompt = client.calls[0][1].content
    assert "Answer Outline:" in prompt
    assert "1. 入口在哪" in prompt
    assert "结论 → 依据" in prompt
    assert "150–350 字" in prompt
    assert "不要在句子中间插入引用标记" in prompt


def test_section_budget_is_stated_as_a_hard_limit() -> None:
    client = FakeLLMClient("第一节内容。\n[C1]")

    AnswerGenerator(client).generate(
        "问题",
        bundle("C1"),
        outline="Answer Outline:\n1. 入口在哪",
    )

    prompt = client.calls[0][1].content
    assert "这是硬性上限" in prompt
    assert "每写完一节" in prompt
    assert "不要用罗列细节来充篇幅" in prompt


def test_without_outline_the_prompt_carries_no_answer_chain() -> None:
    client = FakeLLMClient("第一节内容。\n[C1]")

    AnswerGenerator(client).generate("问题", bundle("C1"))

    prompt = client.calls[0][1].content
    assert "Answer Outline:" not in prompt
    assert "150–350 字" not in prompt


def test_describe_sections_measures_each_section() -> None:
    answer = "导语。\n\n## 第一节\n\n" + "字" * 200 + "\n\n## 第二节\n\n" + "字" * 400

    described = describe_sections(answer)

    assert described["count"] == 2
    assert described["lengths"][0] < described["lengths"][1]
    assert described["max_chars"] == described["lengths"][1]
    assert described["over_budget"] == 1


def test_describe_sections_handles_an_answer_without_sections() -> None:
    assert describe_sections("一段没有小节的回答。") == {
        "count": 0,
        "lengths": [],
        "max_chars": 0,
        "over_budget": 0,
    }


def test_describe_sections_counts_nothing_over_budget_when_all_fit() -> None:
    described = describe_sections("## 甲\n\n" + "字" * 100 + "\n\n## 乙\n\n" + "字" * 100)

    assert described["count"] == 2
    assert described["over_budget"] == 0


def test_empty_context_returns_fixed_answer_without_calling_llm() -> None:
    client = FakeLLMClient("must not be used")
    empty = bundle()

    result = AnswerGenerator(client).generate("问题", empty)

    assert result.answer == EMPTY_CONTEXT_ANSWER
    assert result.used_citations == []
    assert client.calls == []


def test_citations_are_deduplicated_in_first_appearance_order() -> None:
    assert extract_citations("先看 [C2]，再看 [C1]，重复 [C2]。") == ["C2", "C1"]
    assert extract_citations("[c1] 不是引用，但 [C01] 和 [C0] 会进入校验。") == [
        "C01",
        "C0",
    ]


def test_invalid_citations_are_dropped_without_losing_the_answer() -> None:
    client = FakeLLMClient("已有事实。\n[C1][C7][C0]")

    result = AnswerGenerator(client).generate("问题", bundle("C1", "C2"))

    assert result.answer == "已有事实。"
    assert result.used_citations == ["C1"]
    assert result.invalid_citations == ["C7", "C0"]
    assert result.zero_valid_citation is False


def test_answer_without_citations_is_allowed_and_flagged() -> None:
    result = AnswerGenerator(FakeLLMClient("当前证据不足以确认。")).generate(
        "问题", bundle("C1")
    )

    assert result.used_citations == []
    assert result.answer == "当前证据不足以确认。"
    assert result.zero_valid_citation is True


def test_strip_citations_removes_markers_and_the_evidence_line() -> None:
    text, labels = strip_citations("结论第一句。\n[C1][C3]\n\n结论第二句。\n[C2]")

    assert text == "结论第一句。\n\n结论第二句。"
    assert labels == ["C1", "C3", "C2"]


def test_strip_citations_handles_inline_markers() -> None:
    text, labels = strip_citations("入口见 [C1]。")

    assert text == "入口见。"
    assert labels == ["C1"]


def test_empty_llm_answer_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="LLM returned an empty answer"):
        AnswerGenerator(FakeLLMClient("   ")).generate("问题", bundle("C1"))


def test_partial_answer_prompt_treats_missing_aspects_as_evidence_gaps() -> None:
    client = FakeLLMClient("现有证据只支持入口位置 [C1]，事务原因无法确认。")

    result = AnswerGenerator(client).generate_partial(
        "入口和事务原因是什么？",
        bundle("C1"),
        ["[DOCUMENT] 缺少事务设计依据"],
    )

    assert result.used_citations == ["C1"]
    prompt = client.calls[0][1].content
    assert "Known Evidence Gaps" in prompt
    assert "[DOCUMENT] 缺少事务设计依据" in prompt
    assert "不是项目事实" in prompt
    assert "不得猜测" in prompt


def test_partial_answer_requires_a_missing_aspect() -> None:
    with pytest.raises(ValueError, match="requires at least one"):
        AnswerGenerator(FakeLLMClient("unused")).generate_partial(
            "问题", bundle("C1"), []
        )


def test_source_formatting_uses_real_citation_metadata() -> None:
    context_bundle = bundle("C1", "C2")

    assert format_source(context_bundle.items[0].citation) == (
        "[C1] Service.java:10-20 — Service#run"
    )
    assert format_source(context_bundle.items[1].citation) == (
        "[C2] design.md > 设计 > 事务"
    )
