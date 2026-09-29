from __future__ import annotations

import json

import pytest

from devcontext.answer import AnswerReviewer, GroundedDraft, fallback_answer_plan
from devcontext.context import ContextBuilder
from devcontext.models import SearchResult
from devcontext.planning import QuestionPlan, SubQuestion


class FakeClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def generate(self, messages) -> str:
        return json.dumps(self.response, ensure_ascii=False)


def setup():
    result = SearchResult(
        1, "CODE", "METHOD", "Service.java", "evidence", 1, 2,
        "Service", "run", None, None, 1.0,
    )
    bundle = ContextBuilder().build("问题", [result])
    plan = QuestionPlan(
        "问题", "理解", (SubQuestion("SQ1", "实现", "主线", "代码", ("CODE",)),),
        "detailed", answer_goal="理解当前实现",
    )
    return bundle, plan, fallback_answer_plan(plan, bundle, ())


def test_reviewer_returns_one_grounded_revision() -> None:
    bundle, plan, answer_plan = setup()
    reviewer = AnswerReviewer(lambda: FakeClient({
        "accepted": False,
        "issues": [{"issue_type": "REPETITION", "description": "删除重复段落"}],
        "final_answer_with_citations": "修订后的回答 [C1]",
    }))

    result = reviewer.review(
        "问题", plan, answer_plan, GroundedDraft("草稿 [C1]", ("C1",), ()), bundle
    )

    assert result.issues[0].issue_type == "REPETITION"
    assert result.final_answer_with_citations.endswith("[C1]")


def test_reviewer_rejects_an_invented_citation() -> None:
    bundle, plan, answer_plan = setup()
    reviewer = AnswerReviewer(lambda: FakeClient({
        "accepted": True,
        "issues": [],
        "final_answer_with_citations": "错误引用 [C9]",
    }))

    with pytest.raises(ValueError, match="invalid citation"):
        reviewer.review(
            "问题", plan, answer_plan, GroundedDraft("草稿", (), ()), bundle
        )
