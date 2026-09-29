from __future__ import annotations

import json

import pytest

from devcontext.agentic import ContextSufficiencyChecker
from devcontext.agentic.sufficiency import SUB_QUESTION_SUFFICIENCY_SYSTEM_PROMPT
from devcontext.llm import LLMMessage
from devcontext.models import Citation, ContextBundle, ContextItem
from devcontext.planning import SubQuestion

QUERY = "订单关闭为何这样设计"


class FakeLLMClient:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[list[LLMMessage]] = []

    def generate(self, messages: list[LLMMessage]) -> str:
        self.calls.append(list(messages))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def checker(response: str | Exception) -> tuple[ContextSufficiencyChecker, FakeLLMClient]:
    client = FakeLLMClient(response)
    return ContextSufficiencyChecker(lambda: client), client


def sub_question(
    identifier: str, *sources: str, description: str | None = None
) -> SubQuestion:
    return SubQuestion(
        id=identifier,
        question=f"{identifier} 的问题",
        purpose=f"{identifier} 的目的",
        evidence_description=description or f"{identifier} 要找的证据",
        preferred_sources=tuple(sources),
    )


def bundle(*items: tuple[int, str]) -> ContextBundle:
    """items are (chunk_id, source_type) pairs."""
    built = [
        ContextItem(
            citation=Citation(
                label=f"C{index}",
                source_type=source_type,
                file_path="Service.java" if source_type == "CODE" else "design.md",
            ),
            content=f"evidence {chunk_id}",
            chunk_id=chunk_id,
            chunk_type="METHOD" if source_type == "CODE" else "DOCUMENT_SECTION",
            score=0.5,
            retrieval_rank=index,
        )
        for index, (chunk_id, source_type) in enumerate(items, start=1)
    ]
    return ContextBundle(
        query=QUERY,
        items=built,
        rendered_text="\n\n".join(
            f"[{item.citation.label}] {item.content}" for item in built
        ),
        total_chars=0,
        max_chars=6000,
        truncated=False,
    )


def statuses_payload(*flags: bool) -> str:
    return json.dumps(
        {
            "statuses": [
                {"satisfied": flag, "reason": f"判断 {index}"}
                for index, flag in enumerate(flags, start=1)
            ]
        },
        ensure_ascii=False,
    )


# --- stage 1: deterministic, per sub_question, no model call -------------------


def test_sub_question_with_no_evidence_fails_without_calling_the_model() -> None:
    subject, client = checker(statuses_payload(True, True))
    sub_questions = [sub_question("SQ1", "CODE"), sub_question("SQ2", "DOCUMENT")]

    result = subject.check_sub_questions(
        QUERY,
        sub_questions,
        {"SQ1": [1], "SQ2": []},
        bundle((1, "CODE")),
    )

    assert result.enough is False
    assert result.decision_source == "rules"
    assert client.calls == []
    assert [aspect.sub_question_id for aspect in result.missing_aspects] == ["SQ2"]
    assert [status.satisfied for status in result.statuses] == [True, False]


def test_truncated_evidence_is_reported_differently_from_missing_evidence() -> None:
    subject, _ = checker(statuses_payload(True, True))
    sub_questions = [sub_question("SQ1", "CODE"), sub_question("SQ2", "CODE")]

    result = subject.check_sub_questions(
        QUERY,
        sub_questions,
        {"SQ1": [1], "SQ2": [7]},  # ER2 retrieved chunk 7, but it is not in the bundle
        bundle((1, "CODE")),
    )

    reasons = {status.sub_question_id: status.reason for status in result.statuses}
    assert result.enough is False
    assert "预算截断" in reasons["SQ2"]
    assert "没有检索到任何证据" not in reasons["SQ2"]


def test_partially_truncated_item_does_not_count_as_sufficient() -> None:
    subject, client = checker(statuses_payload(True))
    context = bundle((1, "CODE"))
    context.items[0].truncated = True

    result = subject.check_sub_questions(
        QUERY, [sub_question("SQ1", "CODE")], {"SQ1": [1]}, context
    )

    assert result.enough is False
    assert client.calls == []


def test_mixed_requirement_needs_both_sources_before_semantic_judging() -> None:
    subject, client = checker(statuses_payload(True))

    result = subject.check_sub_questions(
        QUERY,
        [sub_question("SQ1", "CODE", "DOCUMENT")],
        {"SQ1": [1]},
        bundle((1, "CODE")),
    )

    assert result.enough is False
    assert client.calls == []


def test_source_mismatch_is_reported_as_a_mismatch() -> None:
    subject, _ = checker(statuses_payload(True, True))
    sub_questions = [sub_question("SQ1", "DOCUMENT")]

    result = subject.check_sub_questions(
        QUERY, sub_questions, {"SQ1": [1]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert "不符" in result.statuses[0].reason


def test_a_two_source_sub_question_contributes_one_aspect_per_source() -> None:
    subject, _ = checker(statuses_payload(True))
    sub_questions = [sub_question("SQ1", "CODE", "DOCUMENT")]

    result = subject.check_sub_questions(
        QUERY, sub_questions, {"SQ1": []}, bundle()
    )

    assert [aspect.source_type for aspect in result.missing_aspects] == [
        "CODE",
        "DOCUMENT",
    ]
    assert {aspect.sub_question_id for aspect in result.missing_aspects} == {"SQ1"}


def test_evidence_from_another_sub_question_does_not_satisfy_this_one() -> None:
    subject, _ = checker(statuses_payload(True, True))
    sub_questions = [sub_question("SQ1", "CODE"), sub_question("SQ2", "CODE")]

    # SQ1's chunk is in the bundle; SQ2's is not. Same source type must not leak.
    result = subject.check_sub_questions(
        QUERY, sub_questions, {"SQ1": [1], "SQ2": [2]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert [status.sub_question_id for status in result.statuses if not status.satisfied] == [
        "SQ2"
    ]


# --- stage 2: one model call, one verdict per sub_question --------------------


def test_all_sub_questions_present_asks_the_model_once() -> None:
    subject, client = checker(statuses_payload(True, True))
    sub_questions = [sub_question("SQ1", "CODE"), sub_question("SQ2", "DOCUMENT")]

    result = subject.check_sub_questions(
        QUERY,
        sub_questions,
        {"SQ1": [1], "SQ2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    )

    assert result.enough is True
    assert result.decision_source == "llm"
    assert result.missing_aspects == ()
    assert [status.sub_question_id for status in result.statuses] == ["SQ1", "SQ2"]
    assert len(client.calls) == 1
    assert client.calls[0][0].content == SUB_QUESTION_SUFFICIENCY_SYSTEM_PROMPT
    assert "SQ1" in client.calls[0][1].content
    assert "[C1]" in client.calls[0][1].content


def test_one_unsatisfied_sub_question_makes_the_whole_result_insufficient() -> None:
    subject, _ = checker(statuses_payload(True, False))
    sub_questions = [sub_question("SQ1", "CODE"), sub_question("SQ2", "DOCUMENT")]

    result = subject.check_sub_questions(
        QUERY,
        sub_questions,
        {"SQ1": [1], "SQ2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    )

    assert result.enough is False
    assert result.decision_source == "llm"
    assert [aspect.sub_question_id for aspect in result.missing_aspects] == ["SQ2"]
    assert "SQ2" in result.reason


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"items": []}',
        '{"statuses": [], "extra": 1}',
        '{"statuses": {}}',
        statuses_payload(True),  # one status for two sub_questions
        statuses_payload(True, True, True),
        '{"statuses": [{"satisfied": true}]}',
        '{"statuses": [{"satisfied": true, "reason": "", "extra": 1}]}',
        '{"statuses": [{"satisfied": "yes", "reason": "r"}, {"satisfied": true, "reason": "r"}]}',
        '{"statuses": [{"satisfied": true, "reason": "   "}, {"satisfied": true, "reason": "r"}]}',
    ],
)
def test_invalid_statuses_fail_closed(response: str) -> None:
    subject, _ = checker(response)
    sub_questions = [sub_question("SQ1", "CODE"), sub_question("SQ2", "DOCUMENT")]

    result = subject.check_sub_questions(
        QUERY,
        sub_questions,
        {"SQ1": [1], "SQ2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    )

    assert result.enough is False
    assert result.decision_source == "fallback"
    assert [status.sub_question_id for status in result.statuses] == ["SQ1", "SQ2"]
    assert {aspect.sub_question_id for aspect in result.missing_aspects} == {
        "SQ1",
        "SQ2",
    }


def test_network_failure_fails_closed_without_leaking_exception() -> None:
    subject, _ = checker(RuntimeError("secret-key-value"))
    sub_questions = [sub_question("SQ1", "CODE")]

    result = subject.check_sub_questions(
        QUERY, sub_questions, {"SQ1": [1]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert result.decision_source == "fallback"
    assert "secret-key-value" not in result.reason


def test_missing_client_factory_fails_closed() -> None:
    subject = ContextSufficiencyChecker()

    result = subject.check_sub_questions(
        QUERY, [sub_question("SQ1", "CODE")], {"SQ1": [1]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert result.decision_source == "fallback"


# --- argument guards ---------------------------------------------------------


def test_empty_query_is_rejected() -> None:
    subject, _ = checker(statuses_payload(True))

    with pytest.raises(ValueError, match="query must not be empty"):
        subject.check_sub_questions("   ", [sub_question("SQ1", "CODE")], {}, bundle())


def test_no_sub_questions_is_rejected() -> None:
    subject, _ = checker(statuses_payload(True))

    with pytest.raises(ValueError, match="at least one sub-question"):
        subject.check_sub_questions(QUERY, [], {}, bundle())


# --- serialization -----------------------------------------------------------


def test_statuses_serialize_into_the_result_dict() -> None:
    subject, _ = checker(statuses_payload(True, False))
    sub_questions = [sub_question("SQ1", "CODE"), sub_question("SQ2", "DOCUMENT")]

    payload = subject.check_sub_questions(
        QUERY,
        sub_questions,
        {"SQ1": [1], "SQ2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    ).to_dict()

    assert payload["statuses"] == [
        {"sub_question_id": "SQ1", "satisfied": True, "reason": "判断 1"},
        {"sub_question_id": "SQ2", "satisfied": False, "reason": "判断 2"},
    ]
    assert payload["missing_aspects"][0]["sub_question_id"] == "SQ2"
