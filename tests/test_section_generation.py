from __future__ import annotations

import pytest

from devcontext.agentic import EvidencePackage, RequirementCoverage
from devcontext.context import ContextBuilder, EvidenceWorkspace
from devcontext.context.budget import ModelCapabilities
from devcontext.context.views import context_item_from_ref
from devcontext.evidence import EvidenceCandidate
from devcontext.explanation import (
    ClaimPlan,
    DraftSection,
    ExplanationPlan,
    ExplanationSection,
    SectionComposer,
    TeachingExplanationWorkflow,
    budget_for,
    grounding_issues,
)
from devcontext.explanation.writer import TeachingWriter
from devcontext.models import ContextBundle, SearchResult
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


def make_package(question: str = "详细解释项目的余票桶是如何设计的") -> EvidencePackage:
    requirement = EvidenceRequirement(
        "ER1", "确认余票桶的数据结构", "找到键与字段定义", "CORE", "CURRENT", "CODE"
    )
    plan = EvidencePlan(question, (requirement,))
    workspace = EvidenceWorkspace(question)
    for index in range(1, 5):
        workspace.ingest([EvidenceCandidate(
            "ER1",
            SearchResult(
                index, "CODE", "METHOD", "Service.java", f"body {index} " + "x" * 200,
                1, 2, "Service", f"symbol{index}", None, None, 1.0,
            ),
            "IMPLEMENTATION", "CURRENT", 100,
        )])
    catalog = workspace.freeze()
    coverage = (RequirementCoverage(
        "ER1", "SATISFIED",
        tuple(ref.chunk_id for ref in workspace.for_requirement("ER1")),
        (), "ok", "rules",
    ),)
    items = [context_item_from_ref(ref) for ref in workspace.all()]
    rendered, kept, truncated = ContextBuilder(max_chars=20_000).render_items(items)
    bundle = ContextBundle(question, kept, rendered, len(rendered), 20_000, truncated)
    return EvidencePackage(
        question, plan, bundle, coverage, (), "READY", (), (), catalog, workspace,
    )


class ScriptedClient:
    """Pops one scripted reply per call and records every prompt it was given."""

    last_usage: dict = {}
    model = "fake"
    reasoning_effort = "high"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, messages):
        self.prompts.append(messages[-1].content)
        return self.responses.pop(0)


class FixedPlanner:
    last_client = None

    def __init__(self, plan: ExplanationPlan) -> None:
        self._plan = plan

    def plan(self, request, package) -> ExplanationPlan:
        return self._plan


def claim(**overrides) -> ClaimPlan:
    value = {
        "claim_goal": "断言一项项目事实",
        "claim_type": "PROJECT_FACT",
        "evidence_labels": ("E1",),
        "confidence": "CONFIRMED",
    }
    value.update(overrides)
    return ClaimPlan(**value)


def section(index: int, **overrides) -> ExplanationSection:
    value = {
        "id": f"S{index}",
        "title": f"章节 {index}",
        "section_type": "MECHANISM",
        "teaching_goal": "让读者理解机制",
        "key_points": ("要点",),
        "claim_plans": (claim(),),
        "evidence_labels": (f"E{index}",),
        "teaching_devices": (),
        "depends_on": (),
        "evidence_state": "CONFIRMED",
    }
    value.update(overrides)
    return ExplanationSection(**value)


def make_plan(sections, *, depth: str = "detailed") -> ExplanationPlan:
    return ExplanationPlan(
        answer_goal="理解余票桶",
        direct_answer="它是一道准入闸门。",
        audience_model="熟悉 Redis",
        core_mental_model="Redis 令牌是准入凭证，MySQL 座位才是库存事实",
        primary_strategy="PROBLEM_SOLUTION",
        sections=tuple(sections),
        answer_depth=depth,
    )


def request_for(question: str = "详细解释项目的余票桶是如何设计的", depth=None):
    return UserRequest(question, AnswerOptions(depth, "teach"))


class TestSectionScopedCitations:
    def test_section_cannot_use_an_unbound_citation(self) -> None:
        """E3 exists in the workspace but was not bound to this section."""
        client = ScriptedClient(["桶用 Hash 存储 [E3]。"])
        writer = TeachingWriter(lambda: client)

        draft = writer.write_section(
            "问题", "心智模型", section(1), ContextBundle("q", [], "", 0, 1_000, False)
        )

        assert draft.used_citations == ()
        assert draft.invalid_citations == ("E3",)

    def test_section_keeps_its_own_citations(self) -> None:
        client = ScriptedClient(["桶用 Hash 存储 [E1]。"])
        writer = TeachingWriter(lambda: client)

        draft = writer.write_section(
            "问题", "心智模型", section(1), ContextBundle("q", [], "", 0, 1_000, False)
        )

        assert draft.used_citations == ("E1",)
        assert draft.invalid_citations == ()

    def test_each_section_is_shown_only_its_own_evidence(self) -> None:
        package = make_package()
        plan = make_plan([section(1), section(2)])
        # Two section replies, then the composed answer.
        client = ScriptedClient(["第一节 [E1]。", "第二节 [E2]。", "合成 [E1][E2]。"])
        workflow = TeachingExplanationWorkflow(
            FixedPlanner(plan), TeachingWriter(lambda: client), SectionComposer(lambda: client)
        )

        workflow.run(request_for(), package)

        first, second, _ = client.prompts
        assert "body 1" in first and "body 2" not in first
        assert "body 2" in second and "body 1" not in second

    def test_multi_pass_sections_preserve_citations(self) -> None:
        package = make_package()
        plan = make_plan([section(1), section(2), section(3), section(4)])
        client = ScriptedClient([
            "第一节 [E1]。", "第二节 [E2]。", "第三节 [E3]。", "第四节 [E4]。",
            "合成正文 [E1][E2][E3][E4]。",
        ])
        workflow = TeachingExplanationWorkflow(
            FixedPlanner(plan), TeachingWriter(lambda: client), SectionComposer(lambda: client)
        )

        result = workflow.run(request_for(), package)

        assert result.stats["path"] == "multi_pass"
        assert len(result.section_drafts) == 4
        assert set(result.answer.used_citations) == {"E1", "E2", "E3", "E4"}
        assert result.answer.invalid_citations == []

    def test_composer_does_not_introduce_new_citations(self) -> None:
        package = make_package()
        plan = make_plan([section(1), section(2), section(3), section(4)])
        client = ScriptedClient([
            "第一节 [E1]。", "第二节 [E2]。", "第三节 [E3]。", "第四节 [E4]。",
            "合成时凭空引用了 [E9]。",
        ])
        workflow = TeachingExplanationWorkflow(
            FixedPlanner(plan), TeachingWriter(lambda: client), SectionComposer(lambda: client)
        )

        result = workflow.run(request_for(), package)

        # The composed text is discarded, not patched: a composer that invents
        # evidence has broken the one rule it is trusted on.
        assert "E9" not in result.answer.answer
        assert set(result.answer.used_citations) == {"E1", "E2", "E3", "E4"}
        assert result.answer.invalid_citations == []
        assert result.stats["invalid_citation_count"] == 0


class TestOutputBudget:
    def test_a_deep_answer_is_not_bounded_by_the_old_5000_character_window(self) -> None:
        plan = make_plan([section(i) for i in range(1, 9)], depth="deep")

        budget = budget_for(plan, ModelCapabilities(131_072, 32_768))

        # Eight sections at 2800 tokens is far past 5000 characters' worth.
        assert budget.max_output_tokens > 20_000
        assert budget.allow_multi_pass is True
        assert budget.reason == "sized from the explanation plan"

    def test_no_minimum_length_is_imposed(self) -> None:
        plan = make_plan([section(1)], depth="brief")

        budget = budget_for(plan, ModelCapabilities(131_072, 32_768))

        assert budget.max_output_tokens > 0
        assert budget.preferred_sections == 1
        assert not hasattr(budget, "min_output_tokens")

    def test_brief_locate_answer_stays_brief(self) -> None:
        locate = make_plan(
            [section(1, section_type="DIRECT_ANSWER")], depth="brief"
        )

        budget = budget_for(locate, ModelCapabilities(131_072, 32_768))

        assert budget.allow_multi_pass is False
        assert budget.max_output_tokens < 5_000

    def test_budget_is_capped_by_the_model(self) -> None:
        plan = make_plan([section(i) for i in range(1, 13)], depth="deep")

        budget = budget_for(plan, ModelCapabilities(32_768, 4_096))

        assert budget.max_output_tokens == 4_096
        assert budget.reason == "capped by the model's maximum output"


class TestGroundingIssues:
    def test_project_fact_requires_evidence(self) -> None:
        issues = grounding_issues(
            section(1, claim_plans=(claim(),), evidence_labels=()), "正文。"
        )

        assert [item.issue_type for item in issues] == ["PROJECT_FACT_WITHOUT_EVIDENCE"]

    def test_general_concept_does_not_require_project_citation(self) -> None:
        conceptual = section(
            1,
            claim_plans=(
                ClaimPlan("通用原理", "GENERAL_CONCEPT", (), "PARTIAL"),
            ),
            evidence_labels=(),
        )

        assert grounding_issues(conceptual, "Redis 单条命令是原子的。") == ()

    def test_hypothetical_example_is_labeled(self) -> None:
        example = section(
            1,
            claim_plans=(
                ClaimPlan(
                    "推演风险", "ILLUSTRATIVE_EXAMPLE", (), "UNVERIFIED",
                    ("如果桶已过期",), True,
                ),
            ),
            evidence_labels=(),
        )

        unlabelled = grounding_issues(example, "桶过期后会产生残缺桶。")
        # One defect, one issue: the example check subsumes the conditional one.
        assert [item.issue_type for item in unlabelled] == ["EXAMPLE_NOT_LABELLED"]

        labelled = grounding_issues(example, "假设桶已过期且直接自增，那么会产生残缺桶。")
        assert labelled == ()

    def test_unverified_claim_not_written_as_fact(self) -> None:
        unverified = section(1, evidence_state="UNVERIFIED")

        assert [item.issue_type for item in grounding_issues(unverified, "实现就是这样做的。")] == [
            "UNVERIFIED_WRITTEN_AS_FACT"
        ]
        assert grounding_issues(unverified, "证据中未提供脚本本体，无法确认两阶段逻辑。") == ()

    def test_conditional_claim_is_rendered_with_assumptions(self) -> None:
        conditional = section(
            1,
            claim_plans=(
                ClaimPlan(
                    "条件推演", "PROJECT_INFERENCE", ("E1",), "PARTIAL",
                    ("如果归还时桶已过期",), True,
                ),
            ),
        )

        assert [item.issue_type for item in grounding_issues(conditional, "会发生风险。")] == [
            "CONDITIONAL_NOT_MARKED"
        ]
        assert grounding_issues(conditional, "如果归还时桶已过期，那么这次归还不会生效。") == ()


class TestMultiPassWorkflow:
    def test_a_section_with_no_bound_evidence_is_reported_not_written(self) -> None:
        package = make_package()
        plan = make_plan([section(1), section(2, evidence_labels=("E7",))])
        client = ScriptedClient(["第一节 [E1]。", "合成 [E1]。"])
        workflow = TeachingExplanationWorkflow(
            FixedPlanner(plan), TeachingWriter(lambda: client), SectionComposer(lambda: client)
        )

        result = workflow.run(request_for(), package)

        assert [item.issue_type for item in result.grounding_issues] == [
            "SECTION_WITHOUT_EVIDENCE"
        ]
        assert len(result.section_drafts) == 1

    def test_the_bound_bundle_is_what_the_writer_saw(self) -> None:
        package = make_package()
        plan = make_plan([section(1), section(2), section(3), section(4)])
        client = ScriptedClient([
            "一 [E1]。", "二 [E2]。", "三 [E3]。", "四 [E4]。", "合成 [E1][E2][E3][E4]。",
        ])
        workflow = TeachingExplanationWorkflow(
            FixedPlanner(plan), TeachingWriter(lambda: client), SectionComposer(lambda: client)
        )

        result = workflow.run(request_for(), package)

        assert {item.citation.label for item in result.context_bundle.items} == {
            "E1", "E2", "E3", "E4"
        }
        assert result.budget is not None
