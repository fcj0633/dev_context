from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from devcontext.context.budget import FALLBACK_CAPABILITIES, ModelCapabilities
from devcontext.explanation.micro import (
    MicroExplanationPlanner, TeachingRuntimeOptions, micro_budget, parse_micro_plan,
)
from devcontext.explanation.stream_writer import SectionParser, SingleStreamingTeachingWriter, validate_section
from devcontext.explanation.workflow import TeachingExplanationWorkflow
from devcontext.llm.client import StreamEvent
from devcontext.llm.streaming import StreamFailure, parse_sse
from devcontext.request import AnswerOptions, UserRequest
from test_teaching_workflow import make_package


MICRO_RANGES = {"historical": (1, 16)}


def raw_plan(depth="historical", count=None):
    count = count or 2
    return {"direct_answer": "直接结论", "core_mental_model": "核心模型", "answer_depth": depth,
            "sections": [{"id": f"S{i}", "title": f"问题{i}", "teaching_goal": "解释机制",
                          "key_points": ["要点"], "evidence_labels": ["E1"], "target_tokens": 100}
                         for i in range(1, count + 1)]}


def plan_for(depth="historical", count=None):
    package = make_package()
    request = UserRequest(package.original_query, AnswerOptions(answer_mode="teach"))
    return parse_micro_plan(json.dumps(raw_plan(depth, count)), request, package, FALLBACK_CAPABILITIES)


def section_text(section, citation="E1", body="正文"):
    return f"<<<SECTION:{section.id}>>>\n## {section.title}\n{body} [{citation}]\n<<<END_SECTION:{section.id}>>>\n"


class FakeStream:
    def __init__(self, events):
        self.events = events
        self.calls = 0
        self.last_usage = {}
        self.closed = False

    def generate_stream(self, messages):
        self.calls += 1
        assert "reasoning_content" not in messages[1].content
        try:
            for event in self.events:
                yield event
        finally:
            self.closed = True


def invoke(clients, plan=None, callback=None):
    plan = plan or plan_for()
    iterator = iter(clients)
    writer = SingleStreamingTeachingWriter(lambda: next(iterator))
    return writer.write("问题", plan, make_package().context_bundle,
                        micro_budget(FALLBACK_CAPABILITIES, len(plan.sections)),
                        request_started=time.perf_counter() - 1, on_section=callback)


@pytest.mark.parametrize("depth", MICRO_RANGES)
def test_micro_budget_is_independent_of_section_count(depth):
    low, high = MICRO_RANGES[depth]
    first, last = plan_for(depth, low), plan_for(depth, high)
    assert sum(s.target_tokens for s in first.sections) == sum(s.target_tokens for s in last.sections)
    assert all(s.target_tokens > 0 for s in last.sections)


@pytest.mark.parametrize("mutation", ["count", "evidence", "points", "order", "tokens", "empty"])
def test_invalid_micro_plan(mutation):
    raw = raw_plan()
    if mutation == "count": raw["sections"] = []
    if mutation == "evidence": raw["sections"][0]["evidence_labels"] = ["E99"]
    if mutation == "points": raw["sections"][0]["key_points"] = ["x"] * 4
    if mutation == "order": raw["sections"][0]["id"] = "S2"
    if mutation == "depth": raw["answer_depth"] = "detailed"
    if mutation == "tokens": raw["sections"][0]["target_tokens"] = True
    if mutation == "empty": raw["direct_answer"] = ""
    with pytest.raises(ValueError):
        parse_micro_plan(json.dumps(raw), UserRequest("q", AnswerOptions(answer_mode="teach")),
                         make_package(), FALLBACK_CAPABILITIES)


@pytest.mark.parametrize("width", [1, 2, 7, 19, 10000])
def test_parser_handles_arbitrary_chunking(width):
    plan = plan_for()
    text = "".join(section_text(s) for s in plan.sections)
    parser = SectionParser(s.id for s in plan.sections)
    found = []
    for i in range(0, len(text), width):
        found.extend(parser.feed(text[i:i + width]))
    parser.finish()
    assert [s[0] for s in found] == ["S1", "S2"]


@pytest.mark.parametrize("text", ["hello", "<<<SECTION:S2>>>", "<<<SECTION:S1>>>body",
                                    "<<<SECTION:S1>>>x<<<END_SECTION:S2>>>"])
def test_parser_rejects_malformed_or_missing_sections(text):
    parser = SectionParser(["S1", "S2"])
    with pytest.raises(StreamFailure):
        list(parser.feed(text))
        parser.finish()


@pytest.mark.parametrize("citation", ["E99", "E2", "C1", "E1,E2", "E0"])
def test_validator_rejects_unknown_cross_section_or_malformed_citations(citation):
    section = plan_for().sections[0]
    with pytest.raises(StreamFailure):
        validate_section(section, f"## {section.title}\n正文 [{citation}]", {"E1", "E2"})


def test_validator_requires_body_and_real_input_evidence():
    section = plan_for().sections[0]
    for text, available in [(f"## {section.title}", {"E1"}), (f"## {section.title}\n正文", {"E1"}),
                             (f"## {section.title}\n正文 [E1]", set())]:
        with pytest.raises(StreamFailure):
            validate_section(section, text, available)


def test_writer_one_call_order_usage_and_offsets():
    plan = plan_for()
    client = FakeStream([StreamEvent("content", text="".join(section_text(s) for s in plan.sections)),
                         StreamEvent("usage", usage={"prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 7}),
                         StreamEvent("finish", finish_reason="stop")])
    emitted = []
    sections, trace = invoke([client], plan, emitted.append)
    assert client.calls == 1 and client.closed
    assert sections == tuple(emitted) and [s.section_id for s in sections] == ["S1", "S2"]
    assert trace["completion_status"] == "complete"
    assert trace["visible_output_tokens"] == 13
    assert 1000 <= trace["first_content_token_ms"] <= trace["first_section_ready_ms"] <= trace["stream_finished_ms"]
    assert trace["stream_duration_ms"] == trace["stream_finished_ms"] - trace["stream_started_offset_ms"]
    assert "<<<" not in "".join(s.markdown for s in sections)


def test_no_replay_after_first_section_even_with_failure_in_same_chunk():
    plan = plan_for()
    first = FakeStream([StreamEvent("content", text=section_text(plan.sections[0]) + section_text(plan.sections[1], "E2"))])
    second = FakeStream([])
    emitted = []
    sections, trace = invoke([first, second], plan, emitted.append)
    assert len(sections) == len(emitted) == 1 and second.calls == 0
    assert trace["completion_status"] == "partial" and trace["failed_section"] == "S2"
    assert trace["sections_completed"] == 2 and trace["sections_failed"] == 1


def test_retry_once_before_any_section_and_unknown_usage_remains_null():
    plan = plan_for()
    first = FakeStream([StreamEvent("content", text=section_text(plan.sections[0], "E99"))])
    second = FakeStream([StreamEvent("content", text="".join(section_text(s) for s in plan.sections)),
                         StreamEvent("finish", finish_reason="stop")])
    sections, trace = invoke([first, second], plan)
    assert len(sections) == 2 and trace["stream_retry_count"] == 1
    assert trace["input_tokens"] is None and trace["visible_output_tokens"] is None
    assert trace["sections_completed"] == 2 and trace["sections_failed"] == 0


def test_retry_exhaustion_and_auth_failure():
    client = FakeStream([StreamEvent("error", text="HTTP 401", retryable=False)])
    sections, trace = invoke([client])
    assert not sections and trace["stream_retry_count"] == 0
    first = FakeStream([StreamEvent("error", text="timeout")])
    second = FakeStream([StreamEvent("error", text="timeout")])
    sections, trace = invoke([first, second])
    assert not sections and trace["stream_retry_count"] == 1 and len(trace["attempts"]) == 2


@pytest.mark.parametrize("ending", [[], [StreamEvent("finish", finish_reason="length")]])
def test_missing_or_abnormal_finish_is_partial(ending):
    plan = plan_for()
    client = FakeStream([StreamEvent("content", text=section_text(plan.sections[0])), *ending])
    sections, trace = invoke([client], plan)
    assert len(sections) == 1 and trace["completion_status"] == "partial"


def test_callback_failure_does_not_retry():
    plan = plan_for()
    client = FakeStream([StreamEvent("content", text=section_text(plan.sections[0]))])
    def broken(section):
        raise RuntimeError("consumer")
    _, trace = invoke([client], plan, broken)
    assert trace["stream_retry_count"] == 0 and trace["error"] == "section callback failed"


def sse_bytes():
    payloads = [
        {"choices": [{"delta": {"reasoning_content": "secret thought"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "中文正文"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 9, "completion_tokens": 8, "completion_tokens_details": {"reasoning_tokens": 3}}},
    ]
    return (": keepalive\r\n\r\n" + "".join("data: " + json.dumps(p, ensure_ascii=False) + "\r\n\r\n" for p in payloads)
            + "data: [DONE]\r\n\r\n").encode()


@pytest.mark.parametrize("width", [1, 2, 17, 4096])
def test_sse_utf8_usage_and_reasoning_isolation(width):
    raw = sse_bytes()
    events = list(parse_sse(raw[i:i + width] for i in range(0, len(raw), width)))
    assert [e.type for e in events] == ["content", "usage", "finish"]
    assert events[0].text == "中文正文" and events[1].usage["reasoning_tokens"] == 3
    assert "secret thought" not in repr(events)


@pytest.mark.parametrize("raw", [b"data: {bad}\n\n", b"data: [DONE]\n\n", b"", sse_bytes()[:-16]])
def test_sse_rejects_bad_payload_or_interruption(raw):
    with pytest.raises(StreamFailure):
        list(parse_sse([raw]))


def test_workflow_skips_old_components_and_does_not_fallback():
    plan = plan_for()
    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError(f"old component used: {name}")
    planner_client = SimpleNamespace(generate=lambda messages: json.dumps(raw_plan()), last_usage={})
    micro = MicroExplanationPlanner(lambda: planner_client, FALLBACK_CAPABILITIES)
    stream = FakeStream([StreamEvent("content", text="".join(section_text(s) for s in plan.sections)),
                         StreamEvent("finish", finish_reason="stop")])
    workflow = TeachingExplanationWorkflow(Forbidden(), Forbidden(), Forbidden(), Forbidden(),
        runtime_options=TeachingRuntimeOptions("single_stream"), micro_planner=micro,
        streaming_writer=SingleStreamingTeachingWriter(lambda: stream))
    emitted = []
    result = workflow.run(UserRequest("q", AnswerOptions(answer_mode="teach")), make_package(), on_section=emitted.append)
    assert result.completion_status == "complete" and len(emitted) == 2
    assert [s.stage for s in result.stages] == ["explanation_planning", "teaching_draft"]
    planner_client.generate = lambda messages: "{}"
    failed = workflow.run(UserRequest("q", AnswerOptions(answer_mode="teach")), make_package())
    assert failed.completion_status == "failed" and stream.calls == 1


def test_missing_input_evidence_and_small_model_fail_before_writer():
    plan = plan_for()
    bundle = replace(make_package().context_bundle, items=[])
    writer = SingleStreamingTeachingWriter(lambda: pytest.fail("must not call LLM"))
    with pytest.raises(StreamFailure):
        writer.write("q", plan, bundle, micro_budget(FALLBACK_CAPABILITIES), request_started=time.perf_counter())
    with pytest.raises(ValueError):
        parse_micro_plan(json.dumps(raw_plan()), UserRequest("q", AnswerOptions(answer_mode="teach")),
                         make_package(), ModelCapabilities(context_window=100, max_output_tokens=1))


def test_config_and_cli_validation(monkeypatch):
    from devcontext.config import Settings
    from devcontext.cli import _parser, main
    monkeypatch.delenv("TEACHING_GENERATION_MODE", raising=False)
    assert Settings(_env_file=None).teaching_generation_mode == "multi_pass"
    monkeypatch.setenv("TEACHING_GENERATION_MODE", "single_stream")
    assert Settings(_env_file=None).teaching_generation_mode == "single_stream"
    assert _parser().parse_args(["ask", "q"]).teaching_generation_mode is None
    assert main(["ask", "q", "--answer-mode", "explain", "--teaching-generation-mode", "single_stream"]) == 1
    with pytest.raises(ValueError): TeachingRuntimeOptions("bad")


@pytest.mark.parametrize("override,expected", [(None, "single_stream"), ("multi_pass", "multi_pass"),
                                               ("single_stream", "single_stream")])
def test_cli_overrides_environment_without_changing_workflow_factory_contract(monkeypatch, override, expected):
    import devcontext.cli as cli
    monkeypatch.setenv("TEACHING_GENERATION_MODE", "single_stream")
    seen = []
    def factory(settings, *args):
        seen.append(settings.teaching_generation_mode)
        return SimpleNamespace(run=lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "_planned_workflow", factory)
    monkeypatch.setattr(cli, "_print_agentic_answer", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "_emit_perf", lambda *args: None)
    argv = ["ask", "q", "--answer-mode", "teach"]
    if override:
        argv += ["--teaching-generation-mode", override]
    assert cli.main(argv) == 0 and seen == [expected]


def test_retry_costs_include_both_attempts():
    plan = plan_for()
    usage = StreamEvent("usage", usage={"prompt_tokens": 10, "completion_tokens": 20, "reasoning_tokens": 4})
    first = FakeStream([usage, StreamEvent("error", text="timeout")])
    second = FakeStream([StreamEvent("content", text="".join(section_text(s) for s in plan.sections)),
                         usage, StreamEvent("finish", finish_reason="stop")])
    _, trace = invoke([first, second], plan)
    assert trace["input_tokens"] == 20 and trace["output_tokens"] == 40 and trace["reasoning_tokens"] == 8


def test_incomplete_usage_cannot_be_reported_as_complete_retry_total():
    plan = plan_for()
    first = FakeStream([StreamEvent("error", text="timeout")])
    second = FakeStream([StreamEvent("content", text="".join(section_text(s) for s in plan.sections)),
                         StreamEvent("usage", usage={"prompt_tokens": 10, "completion_tokens": 20}),
                         StreamEvent("finish", finish_reason="stop")])
    _, trace = invoke([first, second], plan)
    assert trace["input_tokens"] is None and trace["output_tokens"] is None
    assert trace["attempts"][1]["usage"]["prompt_tokens"] == 10


def test_perf_report_exposes_stream_without_double_counting():
    from devcontext.observability import PerfRecorder
    from devcontext.observability.report import build_perf_report
    from devcontext.agentic.models import StageUsage
    from devcontext.models import AnswerResult
    stream = {"generation_mode": "single_stream", "sections_emitted": 2, "stream_duration_ms": 100}
    trace = SimpleNamespace(stage_usage=[StageUsage("explanation_planning", 50, "llm"),
                                        StageUsage("teaching_draft", 100, "llm")],
                            teaching=stream, stop_reason="ready", explanation_plan=plan_for().to_dict())
    result = SimpleNamespace(trace=trace, answer_result=AnswerResult("正文 [E1]", ["E1"]))
    report = build_perf_report(recorder=PerfRecorder(), result=result, query="q", answer_mode="teach",
                              depth="brief", top_k=12, wall_clock_ms=200, sample_kind="first_sample", timestamp="now")
    assert report["attributed_ms"] == 150 and report["summary"]["sections"] == 2
    assert report["stream"] is stream and report["generation_mode"] == "single_stream"
    assert report["summary"]["planned_section_count"] == 2
