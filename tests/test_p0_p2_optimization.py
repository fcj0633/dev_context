from __future__ import annotations

import json
import threading
import time
from collections import defaultdict

import pytest

from devcontext.agentic import EvidencePackage, RequirementCoverage
from devcontext.context import EvidenceWorkspace
from devcontext.context.budget import ModelCapabilities
from devcontext.evidence import EvidenceCandidate
from devcontext.explanation import (
    ClaimPlan,
    ExplanationPlan,
    ExplanationSection,
    SectionComposer,
    TeachingExplanationWorkflow,
    TeachingRuntimeOptions,
    ExplanationPlanner,
)
from devcontext.explanation.policy import (
    decide_depth,
    section_budget_for,
    select_locate_evidence,
)
from devcontext.explanation.writer import TeachingWriter
from devcontext.explanation.reviewer import SectionIssue, TeachingReviewResult
from devcontext.models import ContextBundle, SearchResult
from devcontext.observability import (
    PerfRecorder,
    begin_llm_call,
    capture,
    finish_llm_call,
)
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


def _package(count: int = 4, *, query: str = "全面解释 OrderService 调用链"):
    requirements = []
    workspace = EvidenceWorkspace(query)
    coverage = []
    for index in range(1, count + 1):
        requirement = EvidenceRequirement(
            f"ER{index}",
            (
                "定位 OrderService 的入口方法"
                if index == 1
                else f"解释设计原因和异常流程 {index}"
            ),
            (
                "找到 OrderService.handle 的文件和方法"
                if index == 1
                else f"找到机制证据 {index}"
            ),
            "CORE" if index <= 2 else "SUPPORTING",
            "CURRENT",
            "CODE",
        )
        requirements.append(requirement)
        workspace.ingest([EvidenceCandidate(
            requirement.id,
            SearchResult(
                index,
                "CODE",
                "METHOD",
                f"Service{index}.java",
                f"evidence body {index}",
                1,
                2,
                f"Service{index}",
                f"method{index}",
                None,
                None,
                1.0,
            ),
            "IMPLEMENTATION",
            "CURRENT",
            100,
        )])
        coverage.append(RequirementCoverage(
            requirement.id,
            "SATISFIED",
            tuple(ref.chunk_id for ref in workspace.for_requirement(requirement.id)),
            (),
            "ok",
            "rules",
        ))
    plan = EvidencePlan(query, tuple(requirements))
    return EvidencePackage(
        query,
        plan,
        ContextBundle(query, [], "", 0, 20_000, False),
        tuple(coverage),
        (),
        "READY",
        (),
        (),
        workspace.freeze(),
        workspace,
    )


def _section(index: int) -> ExplanationSection:
    label = f"E{index}"
    return ExplanationSection(
        id=f"S{index}",
        title=f"章节 {index}",
        section_type="MECHANISM",
        teaching_goal=f"解释机制 {index}",
        key_points=(f"要点 {index}",),
        claim_plans=(ClaimPlan(
            f"项目事实 {index}", "PROJECT_FACT", (label,), "CONFIRMED"
        ),),
        evidence_labels=(label,),
        target_tokens=500,
    )


def _plan(count: int = 4) -> ExplanationPlan:
    return ExplanationPlan(
        answer_goal="解释调用链",
        direct_answer="调用链由四段组成",
        audience_model="Java 开发者",
        core_mental_model="每段职责由对应证据支撑",
        primary_strategy="EXECUTION_FLOW",
        sections=tuple(_section(index) for index in range(1, count + 1)),
        answer_depth="detailed",
    )


class _FixedPlanner:
    last_client = None
    last_depth_decision = None
    last_section_budget = None
    last_locate_selection = None

    def __init__(self, plan: ExplanationPlan) -> None:
        self.plan_value = plan

    def plan(self, request, package):
        return self.plan_value


class _SentinelClient:
    def generate(self, messages):  # pragma: no cover - failure explains itself
        raise AssertionError("parallel generation read writer.last_client")


class _RecordingClient:
    model = "fake"
    reasoning_effort = "high"
    last_usage = {"prompt_tokens": 10, "completion_tokens": 4}

    def __init__(
        self,
        instance_id: int,
        max_tokens: int,
        attempts: defaultdict[str, int],
        mode: str,
        lock: threading.Lock,
    ) -> None:
        self.instance_id = instance_id
        self.max_tokens = max_tokens
        self.attempts = attempts
        self.mode = mode
        self.lock = lock

    def generate(self, messages):
        payload = json.loads(messages[-1].content)
        label = payload["available_citations"][0]
        revision = "revision_notes" in payload
        attempt_key = f"revision:{label}" if revision else label
        with self.lock:
            self.attempts[attempt_key] += 1
            attempt = self.attempts[attempt_key]
        call_id = begin_llm_call(
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
        )
        # Deliberately finish out of plan order.
        time.sleep({"E1": 0.025, "E2": 0.005}.get(label, 0.012))
        if self.mode == "retry" and label == "E2" and attempt == 1:
            finish_llm_call(call_id, success=False, error="timeout")
            raise TimeoutError("timeout")
        if self.mode == "partial" and label == "E2":
            finish_llm_call(call_id, success=False, error="HTTP 503")
            raise RuntimeError("HTTP 503")
        if self.mode == "all_fail":
            finish_llm_call(call_id, success=False, error="HTTP 503")
            raise RuntimeError("HTTP 503")
        if self.mode == "revision_partial" and revision and label == "E2":
            finish_llm_call(call_id, success=False, error="HTTP 503")
            raise RuntimeError("HTTP 503")
        content = "错误引用 [E99]" if self.mode == "invalid" and label == "E2" else f"正文 [{label}]"
        finish_llm_call(
            call_id,
            input_tokens=10,
            output_tokens=4,
            reasoning_tokens=None,
            visible_output_tokens=4,
            token_detail_available=False,
            finish_reason="stop",
        )
        return content


class _ComposerClient:
    model = "fake"
    reasoning_effort = "high"
    last_usage = {}

    def generate(self, messages):
        payload = json.loads(messages[-1].content.split("\n\n", 1)[0])
        labels = []
        for section in payload["sections"]:
            label = section["id"].replace("S", "E")
            labels.append(f"[{label}]")
        return "合成 " + "".join(labels)


def _parallel_workflow(mode: str = "success"):
    instances = []
    budgets = []
    attempts: defaultdict[str, int] = defaultdict(int)
    lock = threading.Lock()

    def budgeted_factory(max_tokens: int):
        with lock:
            instance_id = len(instances) + 1
            budgets.append(max_tokens)
            client = _RecordingClient(
                instance_id, max_tokens, attempts, mode, lock
            )
            instances.append(client)
        return client

    writer = TeachingWriter(
        lambda: (_ for _ in ()).throw(AssertionError("unbudgeted factory used")),
        budgeted_factory,
    )
    writer.last_client = _SentinelClient()
    workflow = TeachingExplanationWorkflow(
        _FixedPlanner(_plan()),
        writer,
        SectionComposer(lambda: _ComposerClient()),
        capabilities=ModelCapabilities(131_072, 32_768),
        runtime_options=TeachingRuntimeOptions(
            depth_policy="deterministic", section_concurrency=3
        ),
    )
    return workflow, writer, instances, budgets, attempts


class TestTraceV2Concurrency:
    def test_one_hundred_concurrent_ids_are_unique(self) -> None:
        recorder = PerfRecorder()

        def call() -> int:
            call_id = recorder.begin_llm_call(stage="test")
            time.sleep(0.001)
            recorder.finish_llm_call(call_id, success=True)
            return call_id

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=16) as executor:
            ids = list(executor.map(lambda _: call(), range(100)))

        assert sorted(ids) == list(range(1, 101))
        assert len(recorder.llm_calls) == 100
        assert all(call.success for call in recorder.llm_calls)

    def test_exact_wasted_mark_survives_crossed_completion(self) -> None:
        recorder = PerfRecorder()
        first = recorder.begin_llm_call(stage="planner")
        second = recorder.begin_llm_call(stage="writer")
        recorder.finish_llm_call(second)
        recorder.finish_llm_call(first)

        assert recorder.mark_call_wasted(second, "discard", stage="writer")
        assert recorder.llm_call(first).wasted is False
        assert recorder.llm_call(second).wasted_reason == "discard"


class TestDepthAndSectionBudget:
    def test_legacy_policy_defers_to_existing_planner(self) -> None:
        assert decide_depth(
            UserRequest("全面解释", AnswerOptions(None, "teach")), "legacy"
        ) is None

    @pytest.mark.parametrize(
        ("query", "explicit", "expected_depth", "source"),
        [
            ("在哪个类", "deep", "deep", "explicit"),
            ("在哪个类", None, "brief", "locate"),
            ("全面解释原理", None, "detailed", "detail_hint"),
            ("解释订单", None, "standard", "default"),
        ],
    )
    def test_depth_priority(self, query, explicit, expected_depth, source) -> None:
        decision = decide_depth(
            UserRequest(query, AnswerOptions(explicit, "teach")), "deterministic"
        )
        assert (decision.answer_depth, decision.decision_source) == (
            expected_depth,
            source,
        )

    def test_enough_evidence_requires_exact_preferred_count(self) -> None:
        package = _package(5)
        depth = decide_depth(
            UserRequest("解释订单", AnswerOptions(None, "teach")), "deterministic"
        )
        budget = section_budget_for(package, depth)

        assert budget.target_sections == budget.preferred_sections == 3
        assert budget.gap_code is None

    def test_evidence_gap_is_program_generated(self) -> None:
        package = _package(2)
        depth = decide_depth(
            UserRequest("全面解释订单", AnswerOptions(None, "teach")),
            "deterministic",
        )
        budget = section_budget_for(package, depth)

        assert budget.target_sections == 2
        assert budget.preferred_sections == 5
        assert budget.gap_code == "INSUFFICIENT_EVIDENCE_REQUIREMENTS"
        assert "只有 2 个 Requirement" in budget.gap_reason

    def test_locate_binds_only_the_location_requirement(self) -> None:
        package = _package(3, query="OrderService 在哪个类的哪个方法？")
        request = UserRequest(package.original_query, AnswerOptions(None, "teach"))
        depth = decide_depth(request, "deterministic")
        selection = select_locate_evidence(
            request, package, section_budget_for(package, depth)
        )

        assert selection.used is True
        assert selection.requirement_ids == ("ER1",)
        assert selection.evidence_labels == ("E1",)

    def test_zero_evidence_skips_planner_and_writer(self) -> None:
        query = "全面解释订单"
        requirement = EvidenceRequirement(
            "ER1", "解释订单机制", "找到项目证据", "CORE", "CURRENT", "CODE"
        )
        workspace = EvidenceWorkspace(query)
        package = EvidencePackage(
            query,
            EvidencePlan(query, (requirement,)),
            ContextBundle(query, [], "", 0, 20_000, False),
            (RequirementCoverage(
                "ER1", "MISSING", (), ("项目证据",), "missing", "rules"
            ),),
            ("ER1",),
            "EMPTY",
            (),
            (),
            workspace.freeze(),
            workspace,
        )

        class NeverClient:
            def generate(self, messages):  # pragma: no cover
                raise AssertionError("LLM should not be called")

        planner = ExplanationPlanner(lambda: NeverClient(), depth_policy="deterministic")
        workflow = TeachingExplanationWorkflow(
            planner,
            TeachingWriter(lambda: NeverClient()),
            runtime_options=TeachingRuntimeOptions(
                depth_policy="deterministic", section_concurrency=3
            ),
        )
        result = workflow.run(
            UserRequest(query, AnswerOptions(None, "teach")), package
        )

        assert result.explanation_plan.sections == ()
        assert result.stats["plan_section_count"] == 0
        assert result.answer.used_citations == []

    def test_locate_does_not_bind_an_unrelated_core_requirement(self) -> None:
        query = "PaymentHandler 在哪里？"
        requirement = EvidenceRequirement(
            "ER1", "解释余票桶设计取舍", "找到并发设计依据",
            "CORE", "CURRENT", "CODE",
        )
        workspace = EvidenceWorkspace(query)
        workspace.ingest([EvidenceCandidate(
            "ER1",
            SearchResult(
                1, "CODE", "METHOD", "TicketService.java", "bucket design",
                1, 2, "TicketService", "takeToken", None, None, 1.0,
            ),
            "IMPLEMENTATION", "CURRENT", 100,
        )])
        package = EvidencePackage(
            query,
            EvidencePlan(query, (requirement,)),
            ContextBundle(query, [], "", 0, 20_000, False),
            (RequirementCoverage("ER1", "SATISFIED", (1,), (), "ok", "rules"),),
            (), "READY", (), (), workspace.freeze(), workspace,
        )
        request = UserRequest(query, AnswerOptions(None, "teach"))
        depth = decide_depth(request, "deterministic")

        selection = select_locate_evidence(
            request, package, section_budget_for(package, depth)
        )

        assert selection.used is False
        assert selection.evidence_labels == ()
        assert selection.skip_reason == "no related location requirement"


class TestParallelTeachingWriter:
    def test_workers_use_distinct_clients_and_ignore_last_client(self) -> None:
        workflow, writer, instances, budgets, _ = _parallel_workflow()

        with capture() as recorder:
            result = workflow.run(
                UserRequest("全面解释调用链", AnswerOptions(None, "teach")),
                _package(),
            )

        assert len({item.instance_id for item in instances}) == 4
        assert isinstance(writer.last_client, _SentinelClient)
        assert [draft.section_id for draft in result.section_drafts] == [
            "S1", "S2", "S3", "S4"
        ]
        assert all(value < 32_768 for value in budgets)
        calls = [call for call in recorder.llm_calls if call.stage == "teaching_draft"]
        assert len(calls) == 4
        assert {call.parallel_group_id for call in calls} == {"teaching_sections"}
        assert max(call.max_tokens for call in calls) == max(budgets)
        assert all(trace.llm_call_ids for trace in result.section_traces)
        events = sorted(
            [event for call in calls for event in (
                (call.started_offset_ms, 1), (call.ended_offset_ms, -1)
            )],
            key=lambda item: (item[0], item[1]),
        )
        active = peak = 0
        for _, delta in events:
            active += delta
            peak = max(peak, active)
        assert peak == 3

    def test_transient_failure_retries_once_with_precise_call_ids(self) -> None:
        workflow, _, _, _, attempts = _parallel_workflow("retry")

        with capture() as recorder:
            result = workflow.run(
                UserRequest("全面解释调用链", AnswerOptions(None, "teach")),
                _package(),
            )

        trace = next(item for item in result.section_traces if item.section_id == "S2")
        assert attempts["E2"] == 2
        assert trace.attempt_count == 2
        assert len(trace.llm_call_ids) == 2
        calls = [recorder.llm_call(call_id) for call_id in trace.llm_call_ids]
        assert [call.attempt_index for call in calls] == [1, 2]
        assert result.stats["failed_section_ids"] == []

    def test_validation_error_is_not_retried_and_answer_is_partial(self) -> None:
        workflow, _, _, _, attempts = _parallel_workflow("invalid")

        result = workflow.run(
            UserRequest("全面解释调用链", AnswerOptions(None, "teach")),
            _package(),
        )

        trace = next(item for item in result.section_traces if item.section_id == "S2")
        assert attempts["E2"] == 1
        assert trace.attempt_count == 1
        assert trace.error == "CITATION_VALIDATION_ERROR"
        assert result.stats["partial_answer"] is True
        assert result.stats["failed_section_ids"] == ["S2"]
        assert "## 章节 1" in result.answer.answer
        assert "## 章节 2" not in result.answer.answer

    def test_two_transient_failures_do_not_cancel_other_sections(self) -> None:
        workflow, _, _, _, attempts = _parallel_workflow("partial")

        result = workflow.run(
            UserRequest("全面解释调用链", AnswerOptions(None, "teach")),
            _package(),
        )

        assert attempts["E2"] == 2
        assert [draft.section_id for draft in result.section_drafts] == [
            "S1", "S3", "S4"
        ]
        assert result.stats["partial_answer"] is True
        assert result.stats["failed_section_ids"] == ["S2"]

    def test_revision_failure_preserves_original_and_uses_revision_stage(self) -> None:
        workflow, _, _, _, attempts = _parallel_workflow("revision_partial")

        class Reviewer:
            last_client = None

            def review(self, query, plan, drafts):
                return TeachingReviewResult(
                    accepted=False,
                    section_issues=(
                        SectionIssue("S1", "STYLE", "重写"),
                        SectionIssue("S2", "STYLE", "重写"),
                    ),
                    revision_required=("S1", "S2"),
                )

        workflow.reviewer = Reviewer()
        with capture() as recorder:
            result = workflow.run(
                UserRequest("全面解释调用链", AnswerOptions(None, "teach")),
                _package(),
            )

        assert attempts["revision:E1"] == 1
        assert attempts["revision:E2"] == 2
        assert result.stats["failed_section_ids"] == ["S2"]
        assert [draft.section_id for draft in result.section_drafts] == [
            "S1", "S2", "S3", "S4"
        ]
        revisions = [trace for trace in result.section_traces if trace.revision]
        assert [trace.section_id for trace in revisions] == ["S1", "S2"]
        assert next(trace for trace in revisions if trace.section_id == "S2").success is False
        revision_calls = [
            call for call in recorder.llm_calls if call.stage == "teaching_revision"
        ]
        assert revision_calls
        assert {call.parallel_group_id for call in revision_calls} == {
            "teaching_revisions"
        }

    def test_all_section_failures_use_no_draft_fallback(self) -> None:
        workflow, _, _, _, attempts = _parallel_workflow("all_fail")

        result = workflow.run(
            UserRequest("全面解释调用链", AnswerOptions(None, "teach")),
            _package(),
        )

        assert all(attempts[f"E{index}"] == 2 for index in range(1, 5))
        assert result.section_drafts == ()
        assert result.stats["partial_answer"] is False
        assert result.stats["failed_section_ids"] == ["S1", "S2", "S3", "S4"]
        assert result.answer.zero_valid_citation is True
