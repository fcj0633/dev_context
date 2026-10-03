from __future__ import annotations

import json
import time

import pytest

from devcontext.context.budget import FALLBACK_CAPABILITIES, ModelCapabilities
from devcontext.explanation.micro import MICRO_PROMPT, micro_budget, parse_micro_plan
from devcontext.explanation.stream_writer import STREAM_WRITER_PROMPT, SingleStreamingTeachingWriter
from devcontext.explanation.style import TeachingStyleInspector, inspect_answer
from devcontext.explanation.teaching_policy import QUESTION_STRATEGIES, READER_ASSUMPTIONS
from devcontext.llm.client import StreamEvent
from devcontext.request import AnswerOptions, UserRequest
from test_single_stream import FakeStream, invoke, plan_for, raw_plan, section_text
from test_teaching_workflow import make_package


def parse(raw, depth="brief"):
    return parse_micro_plan(json.dumps(raw), UserRequest("q", AnswerOptions(depth, "teach")),
                            make_package(), FALLBACK_CAPABILITIES)


@pytest.mark.parametrize("kind", QUESTION_STRATEGIES)
@pytest.mark.parametrize("reader", READER_ASSUMPTIONS)
def test_question_strategy_and_reader_are_validated_in_one_plan(kind, reader):
    raw = raw_plan()
    raw.update(question_kind=kind, reader_assumption=reader)
    plan = parse(raw)
    assert plan.question_kind == kind and plan.reader_assumption == reader
    assert plan.plan_schema_version == "teaching_v2"
    assert isinstance(plan.to_dict()["sections"][0]["new_terms"], list)
    assert kind in MICRO_PROMPT and QUESTION_STRATEGIES[kind] in MICRO_PROMPT


@pytest.mark.parametrize("mutation", ["takeaway_missing", "takeaway_empty", "takeaway_long", "direct_long", "terms_many",
                                     "terms_empty", "terms_duplicate", "terms_reintroduced", "title_duplicate",
                                     "chars_low", "chars_high", "kind", "reader", "misconceptions"])
def test_invalid_v2_fields(mutation):
    raw = raw_plan()
    first = raw["sections"][0]
    if mutation == "takeaway_missing": del first["reader_takeaway"]
    if mutation == "takeaway_empty": first["reader_takeaway"] = " "
    if mutation == "takeaway_long": first["reader_takeaway"] = "字" * 161
    if mutation == "direct_long": raw["direct_answer"] = "字" * 121
    if mutation == "terms_many": first["new_terms"] = ["事务", "锁", "幂等"]
    if mutation == "terms_empty": first["new_terms"] = [""]
    if mutation == "terms_duplicate": first["new_terms"] = ["Redis", " redis "]
    if mutation == "terms_reintroduced":
        first["new_terms"] = ["补偿"]
        raw["sections"][1]["new_terms"] = ["补偿"]
    if mutation == "title_duplicate": raw["sections"][1]["title"] = first["title"]
    if mutation == "chars_low": first["target_chars"] = 99
    if mutation == "chars_high": first["target_chars"] = 251
    if mutation == "kind": raw["question_kind"] = "RANDOM"
    if mutation == "reader": raw["reader_assumption"] = "expert"
    if mutation == "misconceptions": raw["likely_misconceptions"] = ["x"] * 4
    with pytest.raises(ValueError): parse(raw)


def test_short_plan_does_not_require_padding_to_total_minimum():
    raw = raw_plan("detailed", 6)
    plan = parse(raw, "detailed")
    assert sum(section.target_chars for section in plan.sections) == 600
    assert micro_budget("detailed", ModelCapabilities(131072, 32768)).max_output_tokens == 11000
    assert micro_budget("detailed", FALLBACK_CAPABILITIES).max_output_tokens == 8192
    assert "target_tokens" not in json.dumps(plan.to_dict())


def test_locate_defaults_to_brief_and_explicit_depth_takes_precedence():
    raw = raw_plan()
    raw["question_kind"] = "LOCATE"
    assert parse(raw, None).answer_depth == "brief"
    raw = raw_plan("detailed", 6)
    raw["question_kind"] = "LOCATE"
    assert parse(raw, "detailed").answer_depth == "detailed"
    with pytest.raises(ValueError, match="LOCATE defaults"):
        parse(raw, None)


def test_style_strips_headings_citations_markers_and_formatting_but_preserves_identifiers():
    inspector = TeachingStyleInspector()
    record = inspector.inspect_section("S1", "<<<SECTION:S1>>>\n## 标题\n**座位**由 `seat_status` 判断。[E1]\n<<<END_SECTION:S1>>>", [])
    assert record["section_chars"] == len("座位由seat_status判断。")
    assert record["citation_count"] == 1
    assert record["code_identifier_count"] == 1
    assert record["planned_new_term_count"] == 0


def test_style_counts_long_sentences_sections_and_terminators():
    inspector = TeachingStyleInspector()
    record = inspector.inspect_section("S1", "## 标题\n" + "字" * 321 + "。\n\n短句！还有？")
    assert record["sentence_chars"] == [321, 2, 2]
    assert record["long_sentence_count"] == 1
    codes = {warning["code"] for warning in record["warnings"]}
    assert codes == {"LONG_SECTION", "LONG_SENTENCE"}
    summary = inspector.summarize("complete")
    assert summary["long_sentence_rate"] == 0.3333


def test_style_does_not_split_java_dots_or_commas():
    record = TeachingStyleInspector().inspect_section("S1", "## 标题\n先读 A.run()，再读 B.run()。")
    assert record["sentence_count"] == 1 and record["code_identifier_count"] == 2


def test_style_code_fences_are_separate_from_prose():
    record = TeachingStyleInspector().inspect_section("S1", "## 标题\n先判断座位。\n```java\nseat.update();\n```\n再处理结果。[E2]")
    assert record["section_chars"] == len("先判断座位。再处理结果。")
    assert record["sentence_chars"] == [5, 5]
    assert record["code_block_chars"] > 0 and record["code_identifier_count"] > 0


def test_shared_terms_count_first_introduction_only_and_identifier_runs_warn():
    inspector = TeachingStyleInspector()
    first = inspector.inspect_section("S1", "## 标题\n事务、幂等、补偿。`a()`、`b()`、`c()`、`d()`、`e()`。", ["事务", "幂等"])
    second = inspector.inspect_section("S2", "## 标题\n再说事务边界和补偿。", ["事务边界"])
    assert first["observed_new_term_count"] == 3
    assert second["observed_new_terms"] == ["事务边界"]
    assert {w["code"] for w in first["warnings"]} == {"NEW_TERM_LOAD", "IDENTIFIER_RUN"}
    assert first["max_consecutive_identifiers"] == 5


def test_empty_style_and_historical_plan_fields_are_explicitly_unknown():
    summary = inspect_answer("", "failed")
    assert summary["sections_inspected_count"] == 0 and summary["avg_sentence_chars"] == 0
    summary = inspect_answer("## 一\n正文。\n\n## 二\n正文。", "partial")
    assert summary["completion_status"] == "partial"
    assert summary["planned_new_terms_per_section"] == {"S1": None, "S2": None}


def test_style_warnings_do_not_retry_or_rewrite_and_run_before_callback():
    plan = plan_for()
    client = FakeStream([StreamEvent("content", text="".join(section_text(s, body="字" * 321) for s in plan.sections)),
                         StreamEvent("finish", finish_reason="stop")])
    emitted = []
    sections, trace = invoke([client], plan, emitted.append)
    assert client.calls == 1 and len(emitted) == 2 and trace["stream_retry_count"] == 0
    assert trace["readability"]["warnings"]
    assert trace["readability"]["sections"][0]["target_chars_delta"] > 0
    assert all("字" * 321 in s.markdown for s in sections)


@pytest.mark.parametrize("method", ["inspect_section", "summarize"])
def test_inspector_failure_does_not_lose_valid_output_or_trigger_llm(monkeypatch, method):
    def broken(*args, **kwargs):
        raise RuntimeError("inspector error")
    monkeypatch.setattr(TeachingStyleInspector, method, broken)
    plan = plan_for()
    client = FakeStream([StreamEvent("content", text="".join(section_text(s) for s in plan.sections)),
                         StreamEvent("finish", finish_reason="stop")])
    sections, trace = invoke([client], plan)
    assert len(sections) == 2 and trace["completion_status"] == "complete"
    assert client.calls == 1 and trace["readability"]["inspector_errors"]


def test_partial_style_only_covers_published_sections_and_callback_failure_rolls_back():
    plan = plan_for()
    partial = FakeStream([StreamEvent("content", text=section_text(plan.sections[0])),
                          StreamEvent("finish", finish_reason="stop")])
    _, trace = invoke([partial], plan)
    assert trace["completion_status"] == "partial"
    assert trace["readability"]["sections_inspected_count"] == 1
    def failed_callback(section):
        raise RuntimeError("consumer error")
    client = FakeStream([StreamEvent("content", text=section_text(plan.sections[0]))])
    _, trace = invoke([client], plan, failed_callback)
    assert trace["sections_emitted"] == trace["readability"]["sections_inspected_count"] == 0


def test_writer_has_exact_section_list_and_no_visible_token_quota():
    plan = plan_for()
    captured = []
    class Client(FakeStream):
        def generate_stream(self, messages):
            captured.extend(messages)
            yield from super().generate_stream(messages)
    client = Client([StreamEvent("content", text="".join(section_text(s) for s in plan.sections)),
                     StreamEvent("finish", finish_reason="stop")])
    invoke([client], plan)
    payload = json.loads(captured[1].content)
    assert payload["required_section_ids"] == ["S1", "S2"]
    assert "total_output_budget" not in payload and "target_tokens" not in captured[1].content
    assert "不能合并、跳过或提前结束" in STREAM_WRITER_PROMPT
