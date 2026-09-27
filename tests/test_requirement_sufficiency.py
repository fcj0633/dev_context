from __future__ import annotations

import json

import pytest

from devcontext.agentic import ContextSufficiencyChecker
from devcontext.agentic.sufficiency import REQUIREMENT_SUFFICIENCY_SYSTEM_PROMPT
from devcontext.llm import LLMMessage
from devcontext.models import Citation, ContextBundle, ContextItem
from devcontext.planning import EvidenceRequirement

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


def requirement(
    identifier: str, *sources: str, description: str | None = None
) -> EvidenceRequirement:
    return EvidenceRequirement(
        id=identifier,
        sub_question_id=f"SQ{identifier[-1]}",
        description=description or f"{identifier} 要找的证据",
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


# --- stage 1: deterministic, per requirement, no model call -------------------


def test_requirement_with_no_evidence_fails_without_calling_the_model() -> None:
    subject, client = checker(statuses_payload(True, True))
    requirements = [requirement("ER1", "CODE"), requirement("ER2", "DOCUMENT")]

    result = subject.check_requirements(
        QUERY,
        requirements,
        {"ER1": [1], "ER2": []},
        bundle((1, "CODE")),
    )

    assert result.enough is False
    assert result.decision_source == "rules"
    assert client.calls == []
    assert [aspect.requirement_id for aspect in result.missing_aspects] == ["ER2"]
    assert [status.satisfied for status in result.statuses] == [True, False]


def test_truncated_evidence_is_reported_differently_from_missing_evidence() -> None:
    subject, _ = checker(statuses_payload(True, True))
    requirements = [requirement("ER1", "CODE"), requirement("ER2", "CODE")]

    result = subject.check_requirements(
        QUERY,
        requirements,
        {"ER1": [1], "ER2": [7]},  # ER2 retrieved chunk 7, but it is not in the bundle
        bundle((1, "CODE")),
    )

    reasons = {status.requirement_id: status.reason for status in result.statuses}
    assert result.enough is False
    assert "预算截断" in reasons["ER2"]
    assert "没有检索到任何证据" not in reasons["ER2"]


def test_source_mismatch_is_reported_as_a_mismatch() -> None:
    subject, _ = checker(statuses_payload(True, True))
    requirements = [requirement("ER1", "DOCUMENT")]

    result = subject.check_requirements(
        QUERY, requirements, {"ER1": [1]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert "不符" in result.statuses[0].reason


def test_a_two_source_requirement_contributes_one_aspect_per_source() -> None:
    subject, _ = checker(statuses_payload(True))
    requirements = [requirement("ER1", "CODE", "DOCUMENT")]

    result = subject.check_requirements(
        QUERY, requirements, {"ER1": []}, bundle()
    )

    assert [aspect.source_type for aspect in result.missing_aspects] == [
        "CODE",
        "DOCUMENT",
    ]
    assert {aspect.requirement_id for aspect in result.missing_aspects} == {"ER1"}


def test_evidence_from_another_requirement_does_not_satisfy_this_one() -> None:
    subject, _ = checker(statuses_payload(True, True))
    requirements = [requirement("ER1", "CODE"), requirement("ER2", "CODE")]

    # ER1's chunk is in the bundle; ER2's is not. Same source type must not leak.
    result = subject.check_requirements(
        QUERY, requirements, {"ER1": [1], "ER2": [2]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert [status.requirement_id for status in result.statuses if not status.satisfied] == [
        "ER2"
    ]


# --- stage 2: one model call, one verdict per requirement --------------------


def test_all_requirements_present_asks_the_model_once() -> None:
    subject, client = checker(statuses_payload(True, True))
    requirements = [requirement("ER1", "CODE"), requirement("ER2", "DOCUMENT")]

    result = subject.check_requirements(
        QUERY,
        requirements,
        {"ER1": [1], "ER2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    )

    assert result.enough is True
    assert result.decision_source == "llm"
    assert result.missing_aspects == ()
    assert [status.requirement_id for status in result.statuses] == ["ER1", "ER2"]
    assert len(client.calls) == 1
    assert client.calls[0][0].content == REQUIREMENT_SUFFICIENCY_SYSTEM_PROMPT
    assert "ER1" in client.calls[0][1].content
    assert "[C1]" in client.calls[0][1].content


def test_one_unsatisfied_requirement_makes_the_whole_result_insufficient() -> None:
    subject, _ = checker(statuses_payload(True, False))
    requirements = [requirement("ER1", "CODE"), requirement("ER2", "DOCUMENT")]

    result = subject.check_requirements(
        QUERY,
        requirements,
        {"ER1": [1], "ER2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    )

    assert result.enough is False
    assert result.decision_source == "llm"
    assert [aspect.requirement_id for aspect in result.missing_aspects] == ["ER2"]
    assert "ER2" in result.reason


@pytest.mark.parametrize(
    "response",
    [
        "",
        "not json",
        '{"items": []}',
        '{"statuses": [], "extra": 1}',
        '{"statuses": {}}',
        statuses_payload(True),  # one status for two requirements
        statuses_payload(True, True, True),
        '{"statuses": [{"satisfied": true}]}',
        '{"statuses": [{"satisfied": true, "reason": "", "extra": 1}]}',
        '{"statuses": [{"satisfied": "yes", "reason": "r"}, {"satisfied": true, "reason": "r"}]}',
        '{"statuses": [{"satisfied": true, "reason": "   "}, {"satisfied": true, "reason": "r"}]}',
    ],
)
def test_invalid_statuses_fail_closed(response: str) -> None:
    subject, _ = checker(response)
    requirements = [requirement("ER1", "CODE"), requirement("ER2", "DOCUMENT")]

    result = subject.check_requirements(
        QUERY,
        requirements,
        {"ER1": [1], "ER2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    )

    assert result.enough is False
    assert result.decision_source == "fallback"
    assert [status.requirement_id for status in result.statuses] == ["ER1", "ER2"]
    assert {aspect.requirement_id for aspect in result.missing_aspects} == {
        "ER1",
        "ER2",
    }


def test_network_failure_fails_closed_without_leaking_exception() -> None:
    subject, _ = checker(RuntimeError("secret-key-value"))
    requirements = [requirement("ER1", "CODE")]

    result = subject.check_requirements(
        QUERY, requirements, {"ER1": [1]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert result.decision_source == "fallback"
    assert "secret-key-value" not in result.reason


def test_missing_client_factory_fails_closed() -> None:
    subject = ContextSufficiencyChecker()

    result = subject.check_requirements(
        QUERY, [requirement("ER1", "CODE")], {"ER1": [1]}, bundle((1, "CODE"))
    )

    assert result.enough is False
    assert result.decision_source == "fallback"


# --- argument guards ---------------------------------------------------------


def test_empty_query_is_rejected() -> None:
    subject, _ = checker(statuses_payload(True))

    with pytest.raises(ValueError, match="query must not be empty"):
        subject.check_requirements("   ", [requirement("ER1", "CODE")], {}, bundle())


def test_no_requirements_is_rejected() -> None:
    subject, _ = checker(statuses_payload(True))

    with pytest.raises(ValueError, match="at least one requirement"):
        subject.check_requirements(QUERY, [], {}, bundle())


# --- serialization -----------------------------------------------------------


def test_statuses_serialize_into_the_result_dict() -> None:
    subject, _ = checker(statuses_payload(True, False))
    requirements = [requirement("ER1", "CODE"), requirement("ER2", "DOCUMENT")]

    payload = subject.check_requirements(
        QUERY,
        requirements,
        {"ER1": [1], "ER2": [2]},
        bundle((1, "CODE"), (2, "DOCUMENT")),
    ).to_dict()

    assert payload["statuses"] == [
        {"requirement_id": "ER1", "satisfied": True, "reason": "判断 1"},
        {"requirement_id": "ER2", "satisfied": False, "reason": "判断 2"},
    ]
    assert payload["missing_aspects"][0]["requirement_id"] == "ER2"
