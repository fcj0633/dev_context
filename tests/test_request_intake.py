from __future__ import annotations

import pytest

from devcontext.request import AnswerOptions, UserRequest


def test_request_intake_only_preserves_validated_input_options() -> None:
    request = UserRequest(
        "解释当前占座流程",
        AnswerOptions("detailed", "explain"),
        12_000,
    )

    assert request.to_dict() == {
        "original_query": "解释当前占座流程",
        "answer_options": {
            "depth_override": "detailed",
            "answer_mode": "explain",
        },
        "context_budget_override": 12_000,
    }
    assert set(request.to_dict()) == {
        "original_query", "answer_options", "context_budget_override"
    }


@pytest.mark.parametrize(
    "factory",
    [
        lambda: UserRequest("  "),
        lambda: UserRequest("问题", context_budget_override=0),
        lambda: AnswerOptions("deep", "explain"),
        lambda: AnswerOptions("brief", "unknown"),
    ],
)
def test_request_intake_rejects_invalid_input(factory) -> None:
    with pytest.raises(ValueError):
        factory()
