from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from devcontext.agentic import (
    RequirementCoverage,
    RetrievalController,
    SearchAction,
)
from devcontext.evaluation.retrieval_workflow_runner import RetrievalTraceRecorder
from devcontext.agentic.retrieval_controller import context_budget_for_plan
from devcontext.evidence import SourcePolicy
from devcontext.models import SearchExecution, SearchResult, SearchTimings
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


def evidence_plan() -> EvidencePlan:
    return EvidencePlan(
        "解释占座一致性",
        (
            EvidenceRequirement(
                "ER1", "确认数据库最终更新条件", "找到条件更新和结果检查",
                "CORE", "CURRENT", "CODE",
            ),
            EvidenceRequirement(
                "ER2", "确认设计取舍", "找到当前设计说明",
                "SUPPORTING", "CURRENT", "DOCUMENT",
            ),
        ),
    )


class FakeEvidencePlanner:
    last_client = None

    def plan(self, query: str) -> EvidencePlan:
        return evidence_plan()


class FakeActionPlanner:
    last_client = None

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def plan_actions(self, query, requirements, **kwargs):
        self.calls.append({"requirements": requirements, **kwargs})
        round_index = kwargs["round_index"]
        return tuple(
            SearchAction(
                f"SA{len(self.calls)}-{index}", item.id, round_index,
                "Service update" if round_index else item.target,
                item.source_requirement, "test", "llm",
            )
            for index, item in enumerate(requirements, start=1)
        )


class FakePolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def search_scope_with_trace(self, query: str, scope: str, top_k: int):
        self.calls.append((query, scope))
        identifier = len(self.calls)
        source = "DOCUMENT" if scope == "DOCUMENT" else "CODE"
        return SearchExecution([
            SearchResult(
                identifier, source, "METHOD", "Service.java",
                "conditional update", 1, 2, "Service", "update", None,
                None, 1.0,
            )
        ], SearchTimings())


class FakeCoverage:
    last_client = None

    def __init__(self) -> None:
        self.calls = 0

    def check(self, requirements, context):
        self.calls += 1
        if self.calls == 1:
            return (
                RequirementCoverage(
                    "ER1", "PARTIAL", (1,), ("尚缺结果检查",),
                    "部分满足", "llm",
                ),
                RequirementCoverage(
                    "ER2", "SATISFIED", (2,), (), "满足", "llm",
                ),
            )
        return (
            RequirementCoverage(
                "ER1", "SATISFIED", (1, 3), (), "满足", "llm",
            ),
            RequirementCoverage(
                "ER2", "SATISFIED", (2,), (), "满足", "llm",
            ),
        )


def controller():
    actions = FakeActionPlanner()
    policy = FakePolicy()
    coverage = FakeCoverage()
    return (
        RetrievalController(
            FakeEvidencePlanner(), actions, policy, coverage, SourcePolicy()
        ),
        actions,
        policy,
        coverage,
    )


def test_controller_only_retries_unsatisfied_core_and_freezes_package() -> None:
    subject, actions, policy, coverage = controller()
    outcome = subject.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("brief", "explain")), 10
    )

    assert [[item.id for item in call["requirements"]] for call in actions.calls] == [
        ["ER1", "ER2"], ["ER1"],
    ]
    assert len(policy.calls) == 3
    assert coverage.calls == 2
    assert outcome.package.retrieval_state == "READY"
    assert len(outcome.package.search_history) == 3
    assert outcome.package.context_bundle.max_chars == 16000
    assert actions.calls[1]["discovered_terms"]["ER1"]
    with pytest.raises(FrozenInstanceError):
        outcome.package.search_history = ()  # type: ignore[misc]


def test_answer_depth_does_not_change_retrieval_plan_actions_or_budget() -> None:
    first, _, _, _ = controller()
    second, _, _, _ = controller()
    brief = first.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("brief", "explain")), 10
    ).package
    detailed = second.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("detailed", "explain")), 10
    ).package

    assert brief.evidence_plan == detailed.evidence_plan
    assert [item.query for item in brief.search_history] == [
        item.query for item in detailed.search_history
    ]
    assert brief.context_bundle.max_chars == detailed.context_bundle.max_chars
    assert brief.requirement_coverage == detailed.requirement_coverage


def test_context_budget_is_derived_from_evidence_complexity() -> None:
    compact = EvidencePlan(
        "定位入口",
        (EvidenceRequirement(
            "ER1", "确认入口", "找到入口类和方法",
            "CORE", "CURRENT", "CODE",
        ),),
    )
    balanced = EvidencePlan(
        "解释实现与设计",
        (
            compact.requirements[0],
            EvidenceRequirement(
                "ER2", "确认设计依据", "找到当前设计说明",
                "SUPPORTING", "CURRENT", "DOCUMENT",
            ),
        ),
    )
    broad = EvidencePlan(
        "详细解释完整流程",
        balanced.requirements + (
            EvidenceRequirement(
                "ER3", "确认失败处理", "找到失败分支",
                "CORE", "CURRENT", "CODE",
            ),
            EvidenceRequirement(
                "ER4", "确认验证结果", "找到验证记录",
                "SUPPORTING", "CURRENT", "DOCUMENT",
            ),
        ),
    )

    assert context_budget_for_plan(compact) == 8_000
    assert context_budget_for_plan(balanced) == 16_000
    assert context_budget_for_plan(broad) == 28_000


def test_controller_emits_read_only_evaluation_observations() -> None:
    actions = FakeActionPlanner()
    policy = FakePolicy()
    coverage = FakeCoverage()
    observer = RetrievalTraceRecorder()
    subject = RetrievalController(
        FakeEvidencePlanner(), actions, policy, coverage, SourcePolicy(), observer
    )

    outcome = subject.retrieve(UserRequest("解释占座一致性"), 10)

    assert len(observer.action_candidates) == 3
    assert sorted(observer.round_contexts) == [0, 1]
    assert observer.round_contexts[1] is not outcome.package.context_bundle
    assert observer.action_candidates[0]["results"][0] is not None
