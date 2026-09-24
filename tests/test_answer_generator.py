from __future__ import annotations

import pytest

from devcontext.answer import (
    EMPTY_CONTEXT_ANSWER,
    AnswerGenerator,
    InvalidCitationError,
    extract_citations,
    format_source,
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
    client = FakeLLMClient("事务入口见 [C1]，设计依据见 [C2]。")

    result = AnswerGenerator(client).generate("购票事务如何实现？", context_bundle)

    assert result.answer == "事务入口见 [C1]，设计依据见 [C2]。"
    assert result.used_citations == ["C1", "C2"]
    assert len(client.calls) == 1
    messages = client.calls[0]
    assert [message.role for message in messages] == ["system", "user"]
    assert "只能依据" in messages[0].content
    assert "不得使用模型记忆" in messages[0].content
    assert "购票事务如何实现？" in messages[1].content
    assert context_bundle.rendered_text in messages[1].content
    assert "[C1], [C2]" in messages[1].content


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


def test_invalid_citation_fails_closed() -> None:
    client = FakeLLMClient("已有事实 [C1]，但还引用了 [C7] 和 [C0]。")

    with pytest.raises(InvalidCitationError) as captured:
        AnswerGenerator(client).generate("问题", bundle("C1", "C2"))

    assert captured.value.invalid_citations == ["C7", "C0"]
    assert "[C7], [C0]" in str(captured.value)


def test_answer_without_citations_is_allowed() -> None:
    result = AnswerGenerator(FakeLLMClient("当前证据不足以确认。")).generate(
        "问题", bundle("C1")
    )

    assert result.used_citations == []


def test_empty_llm_answer_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="LLM returned an empty answer"):
        AnswerGenerator(FakeLLMClient("   ")).generate("问题", bundle("C1"))


def test_source_formatting_uses_real_citation_metadata() -> None:
    context_bundle = bundle("C1", "C2")

    assert format_source(context_bundle.items[0].citation) == (
        "[C1] Service.java:10-20 — Service#run"
    )
    assert format_source(context_bundle.items[1].citation) == (
        "[C2] design.md > 设计 > 事务"
    )
