from __future__ import annotations

import json

import pytest

from devcontext.agentic import EvidencePackage, RequirementCoverage
from devcontext.context import ContextBuilder, EvidenceWorkspace
from devcontext.context.budget import ModelCapabilities
from devcontext.context.views import context_item_from_ref
from devcontext.evidence import EvidenceCandidate
from devcontext.explanation import (
    ClaimPlan,
    ExplanationPlan,
    ExplanationSection,
    SectionComposer,
    TeachingExplanationWorkflow,
    TeachingReviewer,
    needs_llm_review,
)
from devcontext.explanation.grounding import GroundingIssue
from devcontext.explanation.reviewer import TeachingReviewResult
from devcontext.explanation.writer import TeachingWriter
from devcontext.models import ContextBundle, SearchResult
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


def make_package(question: str = "详细解释项目的余票桶是如何设计的") -> EvidencePackage:
    plan = EvidencePlan(
        question,
        (EvidenceRequirement(
            "ER1", "确认余票桶的数据结构", "找到键与字段定义", "CORE", "CURRENT", "CODE",
        ),),
    )
    workspace = EvidenceWorkspace(question)
    for index in range(1, 5):
        workspace.ingest([EvidenceCandidate(
            "ER1",
            SearchResult(
                index, "CODE", "METHOD", "Service.java", f"body {index}",
                1, 2, "Service", f"symbol{index}", None, None, 1.0,
            ),
            "IMPLEMENTATION", "CURRENT", 100,
        )])
    catalog = workspace.freeze()
    coverage = (RequirementCoverage(
        "ER1", "SATISFIED",
        tuple(ref.chunk_id for ref in workspace.for_requirement("ER1")), (), "ok", "rules",
    ),)
    items = [context_item_from_ref(ref) for ref in workspace.all()]
    rendered, kept, truncated = ContextBuilder(max_chars=20_000).render_items(items)
    bundle = ContextBundle(question, kept, rendered, len(rendered), 20_000, truncated)
    return EvidencePackage(
        question, plan, bundle, coverage, (), "READY", (), (), catalog, workspace,
    )


class ScriptedClient:
    last_usage: dict = {}
    model = "fake"
    reasoning_effort = "high"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, messages):
        self.prompts.append(messages[-1].content)
        return self.responses.pop(0)


def section(index: int, **overrides) -> ExplanationSection:
    value = {
        "id": f"S{index}",
        "title": f"章节 {index}",
        "section_type": "MECHANISM",
        "teaching_goal": "让读者理解机制",
        "key_points": ("要点",),
        "claim_plans": (
            ClaimPlan("断言", "PROJECT_FACT", (f"E{index}",), "CONFIRMED"),
        ),
        "evidence_labels": (f"E{index}",),
        "teaching_devices": (),
        "depends_on": (),
        "evidence_state": "CONFIRMED",
    }
    value.update(overrides)
    return ExplanationSection(**value)


def make_plan(sections, *, depth: str = "detailed", strategy: str = "PROBLEM_SOLUTION"):
    return ExplanationPlan(
        answer_goal="理解余票桶",
        direct_answer="它是一道准入闸门。",
        audience_model="熟悉 Redis",
        core_mental_model="Redis 令牌是准入凭证，MySQL 座位才是库存事实",
        primary_strategy=strategy,
        sections=tuple(sections),
        answer_depth=depth,
    )


def review_payload(**overrides) -> str:
    value = {
        "accepted": False,
        "global_issues": [],
        "section_issues": [
            {"section_id": "S2", "issue_type": "MISSING_WHY",
             "description": "第二节只说了是什么，没有解释为什么"},
        ],
        "revision_required": ["S2"],
    }
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


class TestReviewParsing:
    def test_reviewer_detects_missing_mental_model(self) -> None:
        payload = json.dumps({
            "accepted": False,
            "global_issues": [
                {"issue_type": "MISSING_MENTAL_MODEL", "description": "没有贯穿全文的核心模型"},
            ],
            "section_issues": [],
            "revision_required": [],
        }, ensure_ascii=False)
        reviewer = TeachingReviewer(lambda: ScriptedClient([payload]))

        result = reviewer.review("q", make_plan([section(1)]), ())

        assert result.accepted is False
        assert result.global_issues[0]["issue_type"] == "MISSING_MENTAL_MODEL"

    def test_reviewer_detects_project_fact_without_support(self) -> None:
        payload = json.dumps({
            "accepted": False,
            "global_issues": [
                {"issue_type": "UNSUPPORTED_CLAIM", "description": "缺少证据"},
            ],
            "section_issues": [],
            "revision_required": [],
        }, ensure_ascii=False)
        reviewer = TeachingReviewer(lambda: ScriptedClient([payload]))

        result = reviewer.review("q", make_plan([section(1)]), ())

        assert result.global_issues[0]["issue_type"] == "UNSUPPORTED_CLAIM"

    def test_reviewer_detects_general_knowledge_as_project_fact(self) -> None:
        payload = json.dumps({
            "accepted": False,
            "global_issues": [
                {"issue_type": "GENERAL_KNOWLEDGE_AS_PROJECT_FACT",
                 "description": "把 Redis 的通用行为写成了本项目实现"},
            ],
            "section_issues": [],
            "revision_required": [],
        }, ensure_ascii=False)
        reviewer = TeachingReviewer(lambda: ScriptedClient([payload]))

        result = reviewer.review("q", make_plan([section(1)]), ())

        assert result.global_issues[0]["issue_type"] == "GENERAL_KNOWLEDGE_AS_PROJECT_FACT"

    def test_an_unknown_issue_type_is_rejected(self) -> None:
        payload = json.dumps({
            "accepted": False,
            "global_issues": [{"issue_type": "MADE_UP", "description": "x"}],
            "section_issues": [],
            "revision_required": [],
        }, ensure_ascii=False)
        reviewer = TeachingReviewer(lambda: ScriptedClient([payload]))

        result = reviewer.review("q", make_plan([section(1)]), ())

        # A malformed review is not a reason to fail the answer.
        assert result.decision_source == "fallback"
        assert result.accepted is True
        assert result.error is not None

    def test_naming_an_unknown_section_is_rejected(self) -> None:
        payload = json.dumps({
            "accepted": False,
            "global_issues": [],
            "section_issues": [
                {"section_id": "S9", "issue_type": "MISSING_WHY", "description": "x"},
            ],
            "revision_required": ["S9"],
        }, ensure_ascii=False)
        reviewer = TeachingReviewer(lambda: ScriptedClient([payload]))

        result = reviewer.review("q", make_plan([section(1)]), ())

        assert result.decision_source == "fallback"

    def test_a_failing_call_accepts_rather_than_blocks(self) -> None:
        class Boom:
            last_usage: dict = {}
            model = "fake"
            reasoning_effort = "high"

            def generate(self, messages):
                raise RuntimeError("network down")

        result = TeachingReviewer(lambda: Boom()).review("q", make_plan([section(1)]), ())

        assert result.accepted is True
        assert result.error is not None


class TestReviewTier:
    def test_brief_uses_deterministic_reviewer_only(self) -> None:
        assert needs_llm_review(make_plan([section(1)], depth="brief"), ()) is False

    def test_location_only_never_gets_an_llm_review(self) -> None:
        plan = make_plan([section(1)], depth="standard", strategy="LOCATION_ONLY")

        assert needs_llm_review(plan, ()) is False

    def test_detailed_always_gets_one(self) -> None:
        assert needs_llm_review(make_plan([section(1)], depth="detailed"), ()) is True
        assert needs_llm_review(make_plan([section(1)], depth="deep"), ()) is True

    def test_standard_only_when_something_is_off(self) -> None:
        plan = make_plan([section(1)], depth="standard")

        assert needs_llm_review(plan, ()) is False
        assert needs_llm_review(
            plan,
            (GroundingIssue("S1", "EXAMPLE_NOT_LABELLED", "x"),),
        ) is True


class TestTargetedRevision:
    def _workflow(self, client: ScriptedClient, plan: ExplanationPlan, review: str):
        return TeachingExplanationWorkflow(
            FixedPlanner(plan),
            TeachingWriter(lambda: client),
            SectionComposer(lambda: client),
            TeachingReviewer(lambda: client),
            capabilities=ModelCapabilities(131_072, 32_768),
        )

    def test_review_targets_only_the_bad_section(self) -> None:
        package = make_package()
        plan = make_plan([section(i) for i in range(1, 5)])
        client = ScriptedClient([
            "一 [E1]。", "二 [E2]。", "三 [E3]。", "四 [E4]。",   # initial sections
            "初稿合成 [E1][E2][E3][E4]。",                         # first compose
            review_payload(),                                     # review
            "二（修订）[E2]。",                                    # S2 only
            "终稿合成 [E1][E2][E3][E4]。",                         # recompose
        ])
        workflow = self._workflow(client, plan, review_payload())

        result = workflow.run(UserRequest("q", AnswerOptions("detailed", "teach")), package)

        assert result.review is not None
        assert result.review.revision_required == ("S2",)
        revised = {d.section_id: d.text_with_citations for d in result.section_drafts}
        assert revised["S2"] == "二（修订）[E2]。"
        # The other three kept their original text.
        assert revised["S1"] == "一 [E1]。"
        assert revised["S3"] == "三 [E3]。"
        assert revised["S4"] == "四 [E4]。"

    def test_review_does_not_rewrite_accepted_sections(self) -> None:
        package = make_package()
        plan = make_plan([section(i) for i in range(1, 5)])
        client = ScriptedClient([
            "一 [E1]。", "二 [E2]。", "三 [E3]。", "四 [E4]。",
            json.dumps({
                "accepted": True, "global_issues": [], "section_issues": [],
                "revision_required": [],
            }, ensure_ascii=False),
            "合成 [E1][E2][E3][E4]。",
        ])
        workflow = self._workflow(client, plan, "")

        result = workflow.run(UserRequest("q", AnswerOptions("detailed", "teach")), package)

        # Four section writes, one review, one compose - nothing regenerated.
        assert len(client.prompts) == 6
        assert [d.text_with_citations for d in result.section_drafts] == [
            "一 [E1]。", "二 [E2]。", "三 [E3]。", "四 [E4]。",
        ]

    def test_the_revision_note_reaches_the_writer(self) -> None:
        package = make_package()
        plan = make_plan([section(i) for i in range(1, 5)])
        client = ScriptedClient([
            "一 [E1]。", "二 [E2]。", "三 [E3]。", "四 [E4]。",
            "初稿合成 [E1][E2][E3][E4]。",
            review_payload(),
            "二（修订）[E2]。",
            "终稿合成 [E1][E2][E3][E4]。",
        ])
        workflow = self._workflow(client, plan, "")

        workflow.run(UserRequest("q", AnswerOptions("detailed", "teach")), package)

        revision_prompt = client.prompts[6]
        assert "revision_notes" in revision_prompt
        assert "MISSING_WHY" in revision_prompt


class FixedPlanner:
    last_client = None

    def __init__(self, plan: ExplanationPlan) -> None:
        self._plan = plan

    def plan(self, request, package) -> ExplanationPlan:
        return self._plan
