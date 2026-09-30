from __future__ import annotations

import json

import pytest

from devcontext.agentic import EvidencePackage, RequirementCoverage
from devcontext.context import EvidenceWorkspace
from devcontext.evidence import EvidenceCandidate
from devcontext.explanation import (
    ExplanationPlanError,
    fallback_explanation_plan,
    parse_explanation_plan,
)
from devcontext.models import ContextBundle, SearchResult
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


REQUIREMENTS = (
    ("ER1", "确认余票桶的数据结构与字段", "找到桶的键与字段定义", "CORE"),
    ("ER2", "确认取令牌与归还令牌的调用点", "找到两个调用点及其顺序", "CORE"),
)
EVIDENCE = (
    ("ER1", "takeToken", "bucket hash body"),
    ("ER2", "purchaseTickets", "lock ordering body"),
)


def make_package(
    question: str = "详细解释项目的余票桶是如何设计的",
    requirements=REQUIREMENTS,
    evidence=EVIDENCE,
) -> EvidencePackage:
    plan = EvidencePlan(
        question,
        tuple(
            EvidenceRequirement(item[0], item[1], item[2], item[3], "CURRENT", "CODE")
            for item in requirements
        ),
    )
    workspace = EvidenceWorkspace(question)
    for index, (requirement_id, symbol, content) in enumerate(evidence, start=1):
        workspace.ingest([EvidenceCandidate(
            requirement_id,
            SearchResult(
                index, "CODE", "METHOD", "Service.java", content,
                1, 2, "Service", symbol, None, None, 1.0,
            ),
            "IMPLEMENTATION", "CURRENT", 100,
        )])
    catalog = workspace.freeze()
    coverage = tuple(
        RequirementCoverage(
            requirement.id,
            "SATISFIED",
            tuple(ref.chunk_id for ref in workspace.for_requirement(requirement.id)),
            (),
            "ok",
            "rules",
        )
        for requirement in plan.requirements
    )
    return EvidencePackage(
        question, plan, ContextBundle(question, [], "", 0, 6_000, False),
        coverage, (), "READY", (), (), catalog, workspace,
    )


def claim(**overrides):
    value = {
        "claim_goal": "断言一项项目事实",
        "claim_type": "PROJECT_FACT",
        "evidence_labels": ["E1"],
        "confidence": "CONFIRMED",
        "assumptions": [],
        "conditional": False,
    }
    value.update(overrides)
    return value


def section(**overrides):
    value = {
        "id": "S1",
        "title": "先说清楚它解决什么问题",
        "section_type": "PROBLEM_SETUP",
        "teaching_goal": "让读者先建立问题",
        "key_points": ["要点"],
        "claim_plans": [claim()],
        "evidence_labels": ["E1"],
        "teaching_devices": ["HYPOTHETICAL_EXAMPLE"],
        "depends_on": [],
        "evidence_state": "CONFIRMED",
        "target_tokens": 400,
    }
    value.update(overrides)
    return value


def payload(**overrides):
    value = {
        "answer_goal": "理解余票桶为什么存在",
        "direct_answer": "它是一道准入闸门。",
        "audience_model": "熟悉 Redis 但不熟悉高并发库存设计",
        "core_mental_model": "Redis 令牌是准入凭证，MySQL 座位才是库存事实",
        "primary_strategy": "PROBLEM_SOLUTION",
        "secondary_strategies": ["TRADEOFF"],
        "prerequisite_concepts": ["Redis Hash"],
        "likely_misconceptions": ["Redis 有余票就代表真的有票"],
        "sections": [section()],
        "unresolved_gaps": [],
        "conflicts": [],
        "answer_depth": "detailed",
    }
    value.update(overrides)
    return value


def parse(value, *, request=None, package=None):
    return parse_explanation_plan(
        json.dumps(value, ensure_ascii=False),
        request or UserRequest("详细解释项目的余票桶是如何设计的", AnswerOptions("detailed", "teach")),
        package or make_package(),
    )


class TestExplanationPlanValidation:
    def test_plan_has_core_mental_model(self) -> None:
        plan = parse(payload())

        assert plan.core_mental_model.startswith("Redis 令牌是准入凭证")

        with pytest.raises(ExplanationPlanError, match="core mental model"):
            parse(payload(core_mental_model="   "))

    def test_plan_does_not_copy_requirements_to_sections(self) -> None:
        """A section named after a requirement means the planner copied the
        investigation list instead of deciding a teaching order."""
        with pytest.raises(ExplanationPlanError, match="must not copy evidence requirements"):
            parse(payload(sections=[section(title=REQUIREMENTS[0][1])]))

    def test_plan_distinguishes_fact_and_example(self) -> None:
        with pytest.raises(ExplanationPlanError, match="PROJECT_FACT requires project evidence"):
            parse(payload(sections=[section(
                claim_plans=[claim(claim_type="PROJECT_FACT", evidence_labels=[])],
            )]))

        with pytest.raises(ExplanationPlanError, match="ILLUSTRATIVE_EXAMPLE must be conditional"):
            parse(payload(sections=[section(
                claim_plans=[claim(
                    claim_type="ILLUSTRATIVE_EXAMPLE", evidence_labels=[],
                    confidence="PARTIAL",
                )],
            )]))

        # A properly labelled hypothesis is accepted.
        hypothesis = section(
            claim_plans=[claim(
                claim_type="ILLUSTRATIVE_EXAMPLE", evidence_labels=[],
                confidence="UNVERIFIED", conditional=True,
                assumptions=["如果桶已过期且直接自增"],
            )],
        )
        plan = parse(payload(sections=[hypothesis]))

        assert plan.sections[0].claim_plans[0].conditional is True

    def test_conditional_claim_cannot_be_confirmed(self) -> None:
        with pytest.raises(ExplanationPlanError, match="cannot be CONFIRMED"):
            parse(payload(sections=[section(
                claim_plans=[claim(
                    claim_type="GENERAL_CONCEPT", evidence_labels=[],
                    confidence="CONFIRMED", conditional=True,
                    assumptions=["如果前提成立"],
                )],
            )]))

    def test_unknown_citation_labels_are_rejected(self) -> None:
        with pytest.raises(ExplanationPlanError, match="unknown citation labels"):
            parse(payload(sections=[section(evidence_labels=["E9"])]))

    def test_location_only_must_not_be_over_planned(self) -> None:
        sections = [
            section(id="S1"),
            section(id="S2", title="第二个章节"),
            section(id="S3", title="第三个章节"),
        ]
        with pytest.raises(ExplanationPlanError, match="over-planned"):
            parse(payload(primary_strategy="LOCATION_ONLY", sections=sections))

        plan = parse(payload(
            primary_strategy="LOCATION_ONLY", sections=[section()],
        ))
        assert len(plan.sections) == 1

    def test_negative_correction_needs_a_misconception_section(self) -> None:
        with pytest.raises(ExplanationPlanError, match="MISCONCEPTION"):
            parse(payload(primary_strategy="NEGATIVE_CORRECTION"))

        plan = parse(payload(
            primary_strategy="NEGATIVE_CORRECTION",
            sections=[section(section_type="MISCONCEPTION")],
        ))
        assert plan.sections[0].section_type == "MISCONCEPTION"

    def test_deep_depth_is_rejected_off_the_teach_path(self) -> None:
        """Reachable without an explicit depth: the caller may leave depth to the
        planner, and the planner may then propose a depth the mode cannot honour."""
        request = UserRequest("解释余票桶", AnswerOptions(None, "explain"))

        with pytest.raises(ExplanationPlanError, match="only available on the teach path"):
            parse(payload(answer_depth="deep"), request=request)

    def test_explicit_depth_override_must_be_honoured(self) -> None:
        request = UserRequest(
            "解释余票桶", AnswerOptions("brief", "teach")
        )

        with pytest.raises(ExplanationPlanError, match="explicit override"):
            parse(payload(answer_depth="detailed"), request=request)


class TestFallbackExplanationPlan:
    def test_uses_failure_scenario_for_an_edge_question(self) -> None:
        package = make_package(question="余票桶在并发失败时会怎样？")

        plan = fallback_explanation_plan(
            UserRequest(package.original_query, AnswerOptions("standard", "teach")),
            package,
        )

        assert plan.primary_strategy == "FAILURE_ANALYSIS"
        assert any(
            item.section_type == "FAILURE_SCENARIO" for item in plan.sections
        )

    def test_locate_question_does_not_overplan(self) -> None:
        package = make_package(question="余票桶在哪个类里？")

        plan = fallback_explanation_plan(
            UserRequest(package.original_query, AnswerOptions("brief", "teach")),
            package,
        )

        assert plan.primary_strategy == "LOCATION_ONLY"
        assert len(plan.sections) == 1

    def test_negative_question_corrects_false_premise(self) -> None:
        package = make_package(question="余票桶是不是库存的事实来源？")

        plan = fallback_explanation_plan(
            UserRequest(package.original_query, AnswerOptions("standard", "teach")),
            package,
        )

        assert plan.primary_strategy == "NEGATIVE_CORRECTION"
        assert plan.sections[0].section_type == "MISCONCEPTION"

    def test_fallback_binds_real_workspace_labels(self) -> None:
        package = make_package()

        plan = fallback_explanation_plan(
            UserRequest(package.original_query, AnswerOptions("standard", "teach")),
            package,
        )

        labels = {label for item in plan.sections for label in item.evidence_labels}
        assert labels <= {"E1", "E2"}
        assert labels, "the fallback should still bind the evidence it has"
        assert plan.decision_source == "fallback"
