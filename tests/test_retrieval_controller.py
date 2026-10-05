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
        # The controller now names the CODE strategy it wants; recorded so a
        # test can assert the stage-aware choice without a real service.
        self.strategies: list[str | None] = []

    def search_scope_with_trace(
        self, query: str, scope: str, top_k: int, *, code_strategy: str | None = None
    ):
        self.calls.append((query, scope))
        self.strategies.append(code_strategy)
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


def test_controller_freezes_a_catalog_covering_every_retrieved_chunk() -> None:
    subject, _, policy, _ = controller()
    outcome = subject.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("brief", "explain")), 10
    )

    catalog = outcome.package.evidence_catalog
    assert catalog is not None
    # FakePolicy mints a fresh chunk id per search call, so all three survive.
    assert len(catalog) == len(policy.calls) == 3
    assert catalog.label_for(1) == "E1"
    assert catalog.label_for(3) == "E3"


def test_catalog_ids_survive_the_round_that_re_finds_them() -> None:
    subject, _, _, _ = controller()
    outcome = subject.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("brief", "explain")), 10
    )

    catalog = outcome.package.evidence_catalog
    assert catalog is not None
    # One chunk, one label: the bundle's positional C-labels are a separate space.
    assert len(set(catalog.by_chunk_id.values())) == len(catalog.by_chunk_id)
    assert all(label.startswith("E") for label in catalog.labels())


class BulkPolicy:
    """Returns several large chunks per action, so a small budget drops the tail."""

    def __init__(self) -> None:
        self.calls = 0

    def search_scope_with_trace(
        self, query: str, scope: str, top_k: int, *, code_strategy: str | None = None
    ):
        self.calls += 1
        return SearchExecution(
            [
                SearchResult(
                    1_000 + self.calls * 10 + index, "CODE", "METHOD", "Service.java",
                    "x" * 4_000, 1, 2, "Service", f"seed{self.calls}-{index}",
                    None, None, 1.0,
                )
                for index in range(3)
            ],
            SearchTimings(),
        )


class FailingPolicy:
    def search_scope_with_trace(
        self, query: str, scope: str, top_k: int, *, code_strategy: str | None = None
    ):
        raise RuntimeError("embedding endpoint unreachable")


class RecordingCoverage:
    """Records how many items the checker was actually shown per requirement."""

    last_client = None

    def __init__(self, first_states: tuple[str, ...] = ("SATISFIED", "SATISFIED")) -> None:
        self.calls = 0
        self.seen: dict[str, int] = {}
        self.first_states = first_states

    def check(self, requirements, view):
        self.calls += 1
        self.seen = {item.id: len(view.items_for(item.id)) for item in requirements}
        states = self.first_states if self.calls == 1 else tuple(
            "SATISFIED" for _ in requirements
        )
        return tuple(
            RequirementCoverage(item.id, state, (), (), "test", "rules")
            for item, state in zip(requirements, states, strict=True)
        )


def test_coverage_is_judged_against_the_workspace_not_the_answer_bundle() -> None:
    """Coverage used to read the answer's bundle, so a presentation budget that
    dropped a requirement's evidence made that requirement look MISSING."""
    policy = BulkPolicy()
    coverage = RecordingCoverage()
    subject = RetrievalController(
        FakeEvidencePlanner(), FakeActionPlanner(), policy, coverage, SourcePolicy()
    )
    outcome = subject.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("brief", "explain"), 6_000), 10
    )

    kept = {item.chunk_id for item in outcome.package.context_bundle.items}
    assert len(kept) < 6, "precondition: the bundle drops most of what was found"
    assert coverage.seen["ER1"] == 3, "coverage must see the whole round, not the bundle"


def test_second_round_seed_includes_evidence_the_budget_dropped() -> None:
    policy = BulkPolicy()
    coverage = RecordingCoverage(first_states=("PARTIAL", "SATISFIED"))
    actions = FakeActionPlanner()
    subject = RetrievalController(
        FakeEvidencePlanner(), actions, policy, coverage, SourcePolicy()
    )
    subject.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("brief", "explain"), 6_000), 10
    )

    seeds = actions.calls[1]["discovered_terms"]["ER1"]
    assert "seed1-0" in seeds
    # seed1-2 was dropped from the answer bundle; the second round must still be
    # able to use it as a search term.
    assert "seed1-2" in seeds


def test_coverage_round_sees_a_single_batch_call() -> None:
    """Requirement-scoped views must not multiply the LLM calls."""
    calls: list[int] = []

    class CountingCoverage(RecordingCoverage):
        def check(self, requirements, view):
            calls.append(len(requirements))
            return super().check(requirements, view)

    subject = RetrievalController(
        FakeEvidencePlanner(), FakeActionPlanner(), BulkPolicy(),
        CountingCoverage(), SourcePolicy(),
    )
    subject.retrieve(UserRequest("解释占座一致性", AnswerOptions("brief", "explain")), 10)

    # One call carrying both requirements, not one call per requirement.
    assert calls == [2]


def test_all_failed_actions_report_retrieval_failed_not_empty() -> None:
    subject = RetrievalController(
        FakeEvidencePlanner(), FakeActionPlanner(), FailingPolicy(),
        RecordingCoverage(), SourcePolicy(),
    )
    package = subject.retrieve(
        UserRequest("解释占座一致性", AnswerOptions("brief", "explain")), 10
    ).package

    assert package.retrieval_state == "RETRIEVAL_FAILED"
    assert all(action.error for action in package.search_history)
    # The message must name the cause, not just the exception class.
    assert any("unreachable" in (action.error or "") for action in package.search_history)
    sufficiency = package.to_legacy_sufficiency()
    assert sufficiency.enough is False
    assert "did not complete" in sufficiency.reason


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


def test_v3_time_reserve_skips_followup_without_upgrading_missing_evidence():
    from devcontext.deadline import request_deadline
    subject, actions, policy, coverage = controller()
    with request_deadline(110):
        result = subject.retrieve(UserRequest('解释占座一致性', AnswerOptions('detailed', 'teach')), 10)
    assert len(actions.calls) == coverage.calls == 1
    assert result.package.requirement_coverage[0].state == 'PARTIAL'
    assert result.package.unresolved_requirements == ('ER1',)
    assert any(s.stage == 'retrieval_followup_skipped' for s in result.stage_usage)
