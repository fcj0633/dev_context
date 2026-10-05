from __future__ import annotations

import copy
import json

import pytest

import devcontext.cli as cli_module
import devcontext.llm.deepseek as deepseek_module
from devcontext.agentic import EvidencePackage, RequirementCoverage
from devcontext.agentic.models import StageUsage
from devcontext.context import ContextBuilder, EvidenceWorkspace
from devcontext.context.views import context_item_from_ref
from devcontext.evidence import EvidenceCandidate
from devcontext.explanation import (
    ClaimPlan,
    ExplanationPlan,
    ExplanationSection,
    SectionComposer,
    TeachingExplanationWorkflow,
)
from devcontext.explanation.reviewer import SectionIssue, TeachingReviewResult
from devcontext.explanation.writer import TeachingWriter
from devcontext.llm import LLMMessage
from devcontext.llm.deepseek import DeepSeekLLMClient
from devcontext.models import ContextBundle, SearchResult
from devcontext.observability import (
    PerfRecorder,
    capture,
    llm_stage,
    mark_last_call_wasted,
    record_embedding_call,
    record_llm_call,
)
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.request import AnswerOptions, UserRequest


# --- a stand-in for the curl subprocess -------------------------------------


class _Completed:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _reply(
    content: str = "答案正文",
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
) -> str:
    return json.dumps({
        "choices": [
            {"finish_reason": finish_reason, "message": {"content": content}}
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    })


@pytest.fixture
def fake_curl(monkeypatch):
    """Install a fake curl so the real client runs without a network."""

    def install(payload: str, returncode: int = 0, status: int = 200) -> None:
        stdout = payload + f"\n__HTTP_STATUS__:{status}"

        def fake_run(*args, **kwargs):
            return _Completed(stdout, returncode)

        monkeypatch.setattr(deepseek_module.shutil, "which", lambda name: "curl")
        monkeypatch.setattr(deepseek_module.subprocess, "run", fake_run)

    return install


def _client() -> DeepSeekLLMClient:
    return DeepSeekLLMClient(
        api_key="test-key",
        model="deepseek-flash",
        reasoning_effort="high",
        max_tokens=4096,
    )


# --- Level 3: the call itself ------------------------------------------------


class TestRecordingIsInert:
    def test_a_call_without_a_capture_still_works(self, fake_curl) -> None:
        fake_curl(_reply())

        client = _client()

        assert client.generate([LLMMessage("user", "问题")]) == "答案正文"
        assert client.last_usage == {"prompt_tokens": 11, "completion_tokens": 7}
        assert client.last_finish_reason == "stop"


class TestLLMCallTrace:
    def test_a_call_records_its_stage_substage_and_tokens(self, fake_curl) -> None:
        fake_curl(_reply())

        with capture() as recorder:
            with llm_stage("teaching_draft", "S3"):
                _client().generate([LLMMessage("user", "问题")])

        call, = recorder.llm_calls
        assert (call.stage, call.substage) == ("teaching_draft", "S3")
        # Actual provider usage, not a character-based estimate.
        assert (call.input_tokens, call.output_tokens) == (11, 7)
        assert call.model == "deepseek-flash"
        assert call.reasoning_effort == "high"
        assert call.max_tokens == 4096
        assert call.success is True
        assert call.finish_reason == "stop"
        assert call.discarded_latency_ms == 0.0

    def test_a_call_outside_any_stage_is_recorded_as_unattributed(self, fake_curl) -> None:
        fake_curl(_reply())

        with capture() as recorder:
            _client().generate([LLMMessage("user", "问题")])

        assert recorder.llm_calls[0].stage == "unattributed"

    def test_a_failed_call_is_recorded_with_its_status(self, fake_curl) -> None:
        fake_curl("boom", returncode=1, status=500)

        with capture() as recorder:
            with llm_stage("coverage_check", "round_1"):
                with pytest.raises(RuntimeError):
                    _client().generate([LLMMessage("user", "问题")])

        call, = recorder.llm_calls
        assert call.success is False
        assert "HTTP 500" in call.error
        assert call.stage == "coverage_check"
        assert (call.input_tokens, call.output_tokens) == (None, None)
        # A failed call spent its seconds and produced nothing.
        assert call.discarded_latency_ms == call.latency_ms

    def test_a_truncated_reply_keeps_its_finish_reason(self, fake_curl) -> None:
        """'length' is a full-price call that returned a partial answer."""
        fake_curl(_reply("half", finish_reason="length"))

        with capture() as recorder:
            with pytest.raises(RuntimeError):
                _client().generate([LLMMessage("user", "问题")])

        call, = recorder.llm_calls
        assert call.finish_reason == "length"
        assert call.success is False


class TestWastedLatency:
    def test_marking_only_touches_a_call_from_the_same_stage(self, fake_curl) -> None:
        fake_curl(_reply())

        with capture() as recorder:
            with llm_stage("explanation_planning"):
                _client().generate([LLMMessage("user", "问题")])

            mark_last_call_wasted("unrelated", stage="coverage_check")
            assert recorder.llm_calls[0].wasted is False

            mark_last_call_wasted("plan rejected", stage="explanation_planning")

        call, = recorder.llm_calls
        assert call.wasted is True
        assert call.wasted_reason == "plan rejected"
        assert call.success is True
        assert call.discarded_latency_ms == call.latency_ms

    def test_a_mark_is_not_applied_twice(self, fake_curl) -> None:
        fake_curl(_reply())

        with capture() as recorder:
            with llm_stage("composer"):
                _client().generate([LLMMessage("user", "问题")])
            mark_last_call_wasted("first", stage="composer")
            mark_last_call_wasted("second", stage="composer")

        assert recorder.llm_calls[0].wasted_reason == "first"

    def test_llm_totals_separate_productive_from_discarded_time(self, fake_curl) -> None:
        fake_curl(_reply(prompt_tokens=100, completion_tokens=40))

        with capture() as recorder:
            with llm_stage("teaching_draft", "S1"):
                _client().generate([LLMMessage("user", "问题")])
            with llm_stage("teaching_review"):
                _client().generate([LLMMessage("user", "问题")])
            mark_last_call_wasted("review unusable", stage="teaching_review")

        totals = recorder.llm_totals()
        assert totals["call_count"] == 2
        assert totals["input_tokens"] == 200
        assert totals["output_tokens"] == 80
        assert totals["discarded_latency_ms"] == recorder.llm_calls[1].latency_ms


class TestRepeatedEmbeddings:
    def test_the_same_query_embedded_twice_is_visible(self) -> None:
        recorder = PerfRecorder()

        with capture(recorder):
            record_embedding_call(query="余票桶", latency_ms=10.0, transport="curl")
            record_embedding_call(query="余票桶", latency_ms=12.0, transport="curl")
            record_embedding_call(query="订单取消", latency_ms=9.0, transport="curl")

        assert recorder.repeated_embeddings() == {"余票桶": 2}
        assert len(recorder.embedding_calls) == 3


# --- Level 2: per-section generation ----------------------------------------


def _package(question: str = "详细解释项目的余票桶是如何设计的") -> EvidencePackage:
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
    """Pops one scripted reply per call. Never touches the recorder, so it also
    stands in for the twenty-odd test doubles that implement only ``generate``."""

    last_usage = {"prompt_tokens": 100, "completion_tokens": 50}
    model = "fake"
    reasoning_effort = "high"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def generate(self, messages):
        return self.responses.pop(0)


class _FixedPlanner:
    last_client = None

    def __init__(self, plan: ExplanationPlan) -> None:
        self._plan = plan

    def plan(self, request, package) -> ExplanationPlan:
        return self._plan


class _RevisionReviewer:
    last_client = None

    def review(self, query, plan, drafts) -> TeachingReviewResult:
        return TeachingReviewResult(
            accepted=False,
            section_issues=(SectionIssue("S2", "UNSUPPORTED_CLAIM", "缺少依据"),),
            revision_required=("S2",),
        )


def _claim() -> ClaimPlan:
    return ClaimPlan(
        "断言一项项目事实", "PROJECT_FACT", ("E1",), "CONFIRMED"
    )


def _section(index: int) -> ExplanationSection:
    return ExplanationSection(
        id=f"S{index}",
        title=f"章节 {index}",
        section_type="MECHANISM",
        teaching_goal="让读者理解机制",
        key_points=("要点",),
        claim_plans=(_claim(),),
        evidence_labels=(f"E{index}",),
    )


def _plan(sections) -> ExplanationPlan:
    return ExplanationPlan(
        answer_goal="理解余票桶",
        direct_answer="它是一道准入闸门。",
        audience_model="熟悉 Redis",
        core_mental_model="Redis 令牌是准入凭证，MySQL 座位才是库存事实",
        primary_strategy="PROBLEM_SOLUTION",
        sections=tuple(sections),

    )


def _request() -> UserRequest:
    return UserRequest("详细解释项目的余票桶是如何设计的", AnswerOptions(answer_mode="teach"))


_FOUR_SECTIONS = [
    "一 [E1]。", "二 [E2]。", "三 [E3]。", "四 [E4]。", "合成 [E1][E2][E3][E4]。",
]


def _workflow(plan, client, *, reviewer=None) -> TeachingExplanationWorkflow:
    return TeachingExplanationWorkflow(
        _FixedPlanner(plan),
        TeachingWriter(lambda: client),
        SectionComposer(lambda: client),
        reviewer,
    )


class TestSectionExecutionTraces:
    def test_every_section_is_timed_individually(self) -> None:
        plan = _plan([_section(index) for index in range(1, 5)])
        client = ScriptedClient(list(_FOUR_SECTIONS))

        with capture() as recorder:
            result = _workflow(plan, client).run(_request(), _package())

        assert [item.section_id for item in recorder.section_executions] == [
            "S1", "S2", "S3", "S4"
        ]
        assert [item.section_id for item in result.section_traces] == [
            "S1", "S2", "S3", "S4"
        ]
        first = recorder.section_executions[0]
        assert first.section_type == "MECHANISM"
        assert first.title == "章节 1"
        assert first.evidence_count == 1
        assert first.revision is False
        assert first.input_tokens == 100
        assert first.output_tokens == 50
        assert first.success is True

    def test_the_sum_of_sections_is_the_teaching_draft_stage(self) -> None:
        """Section traces are children of one stage, not siblings of it."""
        plan = _plan([_section(index) for index in range(1, 5)])
        client = ScriptedClient(list(_FOUR_SECTIONS))

        with capture() as recorder:
            result = _workflow(plan, client).run(_request(), _package())

        draft_stages = [
            stage for stage in result.stages if stage.stage == "teaching_draft"
        ]
        assert len(draft_stages) == 1
        # The stage's own record is what enters the wall-clock budget.
        assert recorder.section_executions
        assert draft_stages[0].latency_ms >= sum(
            item.latency_ms for item in recorder.section_executions
        ) - 1.0

    def test_a_section_without_evidence_is_recorded_as_failed(self) -> None:
        plan = _plan([_section(1), _section(2)])
        client = ScriptedClient(["一 [E1]。", "合成 [E1]。"])
        package = _package()

        # Strip every label but E1 by binding section 2 to a label nothing carries.
        plan = _plan([
            _section(1),
            ExplanationSection(
                id="S2", title="章节 2", section_type="MECHANISM",
                teaching_goal="让读者理解机制", key_points=("要点",),
                claim_plans=(_claim(),), evidence_labels=("E7",),
            ),
        ])

        with capture() as recorder:
            _workflow(plan, client).run(_request(), package)

        skipped = [item for item in recorder.section_executions if not item.success]
        assert [item.section_id for item in skipped] == ["S2"]
        assert skipped[0].error == "SECTION_WITHOUT_EVIDENCE"
        assert skipped[0].latency_ms == 0.0

    def test_revision_sections_are_timed_and_flagged(self) -> None:
        plan = _plan([_section(index) for index in range(1, 5)])
        client = ScriptedClient([
            "一 [E1]。", "二 [E2]。", "三 [E3]。", "四 [E4]。",
            "合成 [E1][E2][E3][E4]。",          # first compose
            "重写二 [E2]。",                      # the revision of S2
            "再次合成 [E1][E2][E3][E4]。",        # compose after revision
        ])

        with capture() as recorder:
            _workflow(plan, client, reviewer=_RevisionReviewer()).run(
                _request(), _package()
            )

        revisions = [item for item in recorder.section_executions if item.revision]
        assert [item.section_id for item in revisions] == ["S2"]
        assert revisions[0].revision_notes_count == 1
        # S1, S2, S3, S4 then the rewritten S2.
        assert len(recorder.section_executions) == 5


class TestInstrumentationChangesNothing:
    """Correction: proving behaviour is unchanged is a mock job, not a live one."""

    def _run(self, profiled: bool):
        plan = _plan([_section(index) for index in range(1, 5)])
        client = ScriptedClient(list(_FOUR_SECTIONS))
        workflow = _workflow(plan, client)
        if profiled:
            with capture():
                return workflow.run(_request(), _package())
        return workflow.run(_request(), _package())

    def test_the_answer_is_byte_identical_when_profiled(self) -> None:
        plain = self._run(False)
        profiled = self._run(True)

        assert plain.answer.answer == profiled.answer.answer
        assert plain.answer.used_citations == profiled.answer.used_citations
        assert plain.answer.invalid_citations == profiled.answer.invalid_citations
        assert plain.stats["path"] == profiled.stats["path"]
        assert plain.stats["invalid_citation_count"] == (
            profiled.stats["invalid_citation_count"]
        )

    def test_every_preexisting_trace_value_is_untouched(self) -> None:
        plain = self._run(False)
        profiled = self._run(True)

        assert _without_section_execution(plain.stats["trace"]) == (
            _without_section_execution(profiled.stats["trace"])
        )

    def test_the_section_trace_records_the_same_sections_either_way(self) -> None:
        plain = self._run(False)
        profiled = self._run(True)

        assert [item.section_id for item in plain.section_traces] == [
            item.section_id for item in profiled.section_traces
        ]


def _without_section_execution(block: dict) -> dict:
    value = copy.deepcopy(block)
    value.pop("section_execution", None)
    return value


# --- Level 1: the request report --------------------------------------------


class _StubTrace:
    def __init__(self, stages) -> None:
        self.stage_usage = stages
        self.stop_reason = "ready"
        self.explanation_plan = {"sections": [{"id": "S1"}, {"id": "S2"}]}


class _StubAnswer:
    def __init__(self, answer: str) -> None:
        self.answer = answer


class _StubResult:
    def __init__(self, stages, answer: str = "正文内容") -> None:
        self.trace = _StubTrace(stages)
        self.answer_result = _StubAnswer(answer)


def _stub_result(answer: str = "正文内容") -> _StubResult:
    return _StubResult([StageUsage("teaching_draft", 1000.0, "llm")], answer)


def _report(recorder, result, *, wall_clock_ms: float = 10_000.0) -> dict:
    from devcontext.observability.report import build_perf_report

    return build_perf_report(
        recorder=recorder,
        result=result,
        query="问题",
        answer_mode="teach",
        depth="detailed",
        top_k=12,
        wall_clock_ms=wall_clock_ms,
        sample_kind="first_sample",
        timestamp="2026-10-01T00:00:00+00:00",
    )


class TestUnattributedUsesOnlyTopLevelStages:
    """Correction C1: child diagnostics must never reduce attributed time."""

    def test_child_records_do_not_enter_the_sum(self) -> None:
        stages = [
            StageUsage("evidence_planning", 1000.0, "llm"),
            StageUsage("teaching_draft", 5000.0, "llm"),
            StageUsage("grounded_draft", 9999.0, "llm"),  # not a top-level stage
        ]
        recorder = PerfRecorder()
        with capture(recorder):
            for _ in range(5):
                record_llm_call(latency_ms=500.0, model="deepseek-flash")

        report = _report(recorder, _StubResult(stages))

        # 1000 + 5000 only. The 9999 legacy stage and 5x500ms of LLM calls are
        # not top-level, so none of them may shrink the unattributed remainder.
        assert report["attributed_ms"] == 6000.0
        assert report["unattributed_ms"] == 4000.0
        assert report["summary"]["llm_calls"] == 5

    def test_section_traces_are_reported_but_not_summed(self) -> None:
        stages = [StageUsage("teaching_draft", 5000.0, "llm")]
        plan = _plan([_section(index) for index in range(1, 4)])
        client = ScriptedClient([
            "一 [E1]。", "二 [E2]。", "三 [E3]。", "合成 [E1][E2][E3]。",
        ])
        recorder = PerfRecorder()
        with capture(recorder):
            _workflow(plan, client).run(_request(), _package())

        report = _report(recorder, _StubResult(stages))

        assert report["summary"]["sections"] == 3
        assert len(report["section_executions"]) == 3
        # The parent stage already covers them; adding them again would double-count.
        assert report["attributed_ms"] == 5000.0

    def test_round_one_stages_are_labelled_and_both_rounds_count(self) -> None:
        stages = [
            StageUsage("coverage_check", 1000.0, "llm", round_index=0),
            StageUsage("coverage_check", 3000.0, "llm", round_index=1),
        ]

        report = _report(PerfRecorder(), _StubResult(stages))

        assert [stage["round_index"] for stage in report["stages"]] == [0, 1]
        assert report["attributed_ms"] == 4000.0


class TestSummaryRendering:
    def test_the_summary_names_the_stages_and_the_unattributed_share(self) -> None:
        from devcontext.observability.report import render_summary

        stages = [
            StageUsage("evidence_planning", 1000.0, "llm"),
            StageUsage("teaching_draft", 5000.0, "llm"),
        ]

        text = render_summary(_report(PerfRecorder(), _StubResult(stages)))

        assert "DevContext Performance" in text
        assert "teaching_draft" in text
        assert "unattributed" in text
        assert "Top bottlenecks" in text


class TestCliPerfSurface:
    def _args(self, *extra: str):
        return cli_module._parser().parse_args(["ask", "问题", *extra])

    def test_without_perf_flags_nothing_is_recorded_or_built(self) -> None:
        sentinel = object()

        result, reports = cli_module._run_ask(
            lambda: sentinel, args=self._args(), top_k=12, answer_mode="teach"
        )

        # Not merely "an empty report" - no report object is built at all, so the
        # ordinary ask path carries none of the tracing overhead.
        assert result is sentinel
        assert reports == []

    def test_repeat_marks_the_first_sample_apart(self) -> None:
        _, reports = cli_module._run_ask(
            _stub_result, args=self._args("--repeat", "3"), top_k=12,
            answer_mode="teach",
        )

        assert [item["sample_kind"] for item in reports] == [
            "first_sample", "repeat_sample", "repeat_sample"
        ]

    def test_a_single_sample_writes_the_documented_object(self, tmp_path) -> None:
        target = tmp_path / "nested" / "perf.json"
        args = self._args("--perf-json", str(target))

        _, reports = cli_module._run_ask(
            _stub_result, args=args, top_k=12, answer_mode="teach"
        )
        cli_module._emit_perf(args, reports)

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["answer_mode"] == "teach"
        assert payload["sample_kind"] == "first_sample"
        assert set(payload["summary"]) >= {
            "llm_calls", "final_answer_chars", "final_answer_tokens_estimated"
        }
        # Actual tokens and the character-based estimate live in different fields.
        assert "final_answer_tokens_estimated" in payload["summary"]
        assert payload["summary"]["final_answer_tokens_estimated"] > 0

    def test_repeated_samples_are_written_under_one_key(self, tmp_path) -> None:
        target = tmp_path / "perf.json"
        args = self._args("--repeat", "2", "--perf-json", str(target))

        _, reports = cli_module._run_ask(
            _stub_result, args=args, top_k=12, answer_mode="teach"
        )
        cli_module._emit_perf(args, reports)

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert [item["sample_kind"] for item in payload["samples"]] == [
            "first_sample", "repeat_sample"
        ]
