from copy import deepcopy
from types import SimpleNamespace
import json
import time

import pytest

from devcontext.context.budget import ModelCapabilities
from devcontext.context.estimator import HeuristicTokenEstimator
from devcontext.deadline import request_deadline, bounded_timeout, RequestDeadlineExceeded
from devcontext.explanation.budget import OutputBudget
from devcontext.explanation.stream_writer import SectionParser, validate_section
from devcontext.explanation.v3.demos import load_demo, WRITER_DEMOS
from devcontext.explanation.v3.errors import PlannerFailure, UnsupportedQuestionKind, EvidencePackUnavailable
from devcontext.explanation.v3.evidence_organizer import demo_pack, build_writer_evidence_pack
from devcontext.explanation.v3.planner import TeachingPlannerV3
from devcontext.explanation.v3.validation import parse_teaching_plan
from devcontext.explanation.v3.writer import TeachingWriterV3
from devcontext.explanation.workflow import TeachingExplanationWorkflow
from devcontext.explanation.runtime import TeachingRuntimeOptions
from devcontext.llm.client import StreamEvent
from devcontext.models import ContextBundle
from devcontext.request import UserRequest, AnswerOptions


def parse(raw):
    return parse_teaching_plan(raw, allowed_labels={"E1", "E2", "E3"}, expected_depth="detailed")


def test_owned_reference_overlap_is_losslessly_deduplicated():
    raw = deepcopy(load_demo("HOW")["blueprint"])
    claim = raw["claims"][0]
    section = next(s for s in raw["answer_structure"] if s["id"] == claim["owner_section"])
    section["may_reference"].append(claim["id"])
    bp = parse(raw)
    result = next(s for s in bp.sections if s["id"] == section["id"])
    assert claim["id"] not in result["may_reference"]
    assert bp.data["claims"][0] == claim
    assert any(w.startswith("OWNED_REFERENCE_DEDUP:") for w in bp.warnings)
    assert claim["id"] in section["may_reference"]  # Input was not mutated.


def test_single_type_wrapper_is_lossless_but_conflicting_wrappers_fail():
    raw = load_demo("WHY")["blueprint"]
    bp = parse({"WHY": raw})
    assert bp.data == parse(raw).data
    assert "BLUEPRINT_WRAPPER_REMOVED:WHY" in bp.warnings
    with pytest.raises(PlannerFailure):
        parse({"HOW": raw})
    with pytest.raises(PlannerFailure):
        parse({"WHY": raw, "HOW": load_demo("HOW")["blueprint"]})


def package(kind="HOW"):
    bp, pack = demo_pack(kind)
    return SimpleNamespace(original_query=load_demo(kind)["input"]["question"], evidence_workspace=None,
        context_bundle=deepcopy(pack.bundle), retrieval_state="READY", requirement_coverage=[],
        evidence_plan=SimpleNamespace(to_dict=lambda: {}), evidence_catalog=None)


@pytest.mark.parametrize("kind", ["WHY", "HOW"])
def test_complete_examples_are_executable_and_match_writer_protocol(kind):
    bp, pack = demo_pack(kind)
    parser = SectionParser(s.id for s in pack.sections)
    # Exercise chunk boundaries, not just a single complete string.
    text = WRITER_DEMOS[kind]
    output = []
    for start in range(0, len(text), 17):
        output.extend(parser.feed(text[start:start+17]))
    parser.finish()
    assert len(output) == len(pack.sections)
    for section, (_, body) in zip(pack.sections, output):
        validate_section(section, body, {x["label"] for x in pack.catalog})
    assert bp.to_dict()["knowledge_boundary"]["unknown"] == (["C4"] if kind == "HOW" else [])


def test_unknown_cannot_be_promoted_to_established_guarantee():
    raw = load_demo("HOW")["blueprint"]
    raw["how_spine_tail"]["established_guarantee_claim_ids"].append("C4")
    with pytest.raises(PlannerFailure, match="unknown cannot"):
        parse(raw)


def test_redundant_supported_form_is_derived_without_reclassifying():
    raw = load_demo("HOW")["blueprint"]
    raw["question_form"] = "WHAT"
    plan = parse(raw)
    assert plan.question_kind == plan.data["question_form"] == "HOW"
    assert any(w.startswith("QUESTION_FORM_DERIVED") for w in plan.warnings)
    assert raw["question_form"] == "WHAT"


def test_unsupported_has_independent_minimal_schema():
    with pytest.raises(UnsupportedQuestionKind) as error:
        parse({"question_form": "WHAT", "question_kind": "UNSUPPORTED", "kind_rationale": "纯定义"})
    assert error.value.question_form == "WHAT"


@pytest.mark.parametrize("change", ["owner", "checkpoint", "goal", "parent"])
def test_broken_cross_references_are_rejected(change):
    raw = load_demo("HOW")["blueprint"]
    if change == "owner":
        raw["claims"][0]["owner_section"] = "S99"
    elif change == "checkpoint":
        raw["answer_structure"][0]["checkpoint_ids"] = ["K99"]
    elif change == "goal":
        for s in raw["answer_structure"]:
            s["goal_ids"] = ["G1"]
    else:
        raw["scenario"]["checkpoints"][0]["parent_checkpoint_id"] = "K3"
    with pytest.raises(PlannerFailure):
        parse(raw)


def test_spine_and_checkpoint_dependencies_enter_pack_without_owner_reference():
    raw = load_demo("HOW")["blueprint"]
    raw["answer_structure"][0]["spine_refs"].append("H3")
    raw["answer_structure"][0]["checkpoint_ids"].append("K3")
    raw["answer_structure"][0]["may_reference"] = []
    pack = build_writer_evidence_pack(parse(raw), package())
    first = pack.sections[0]
    assert "E3" in first.evidence_labels
    assert "C3" in {c["id"] for c in first.contract["dependency_claims"]}


def test_required_evidence_is_not_silently_truncated():
    bp, _ = demo_pack("HOW")
    with pytest.raises(EvidencePackUnavailable, match="超过预算"):
        build_writer_evidence_pack(bp, package(), max_chars=10)
    missing = package()
    missing.context_bundle.items = [i for i in missing.context_bundle.items if i.citation.label != "E3"]
    with pytest.raises(EvidencePackUnavailable, match="E3"):
        build_writer_evidence_pack(bp, missing)
    # A soft character target must not reject necessary data that fits tokens.
    pack = build_writer_evidence_pack(bp, package(), max_chars=10,
        required_token_budget=10000, estimator=HeuristicTokenEstimator())
    assert len(pack.catalog) == 3 and pack.bundle.total_chars > 10


def test_closing_compression_has_global_mental_model_evidence():
    raw = load_demo("HOW")["blueprint"]
    last = raw["answer_structure"][-1]
    last["may_reference"] = []
    # E1 is a global-map dependency, not the tail's own unknown boundary.
    bp = parse(raw)
    pack = build_writer_evidence_pack(bp, package())
    assert "E1" in pack.sections[-1].evidence_labels
    assert "C1" in {c["id"] for c in pack.sections[-1].contract["dependency_claims"]}


def test_prior_context_preserves_owners_and_evidence_for_transitions():
    bp, pack = demo_pack("HOW")
    established = set()
    for section in pack.sections:
        assert set(section.contract["prior_context_claim_ids"]) == established
        for claim in bp.data["claims"]:
            if claim["id"] in established:
                assert set(claim["evidence_labels"]) <= set(section.evidence_labels)
        established.update(c["id"] for c in section.contract["owned_claims"])


class FakeStream:
    def __init__(self, text, *, fail=False):
        self.text = text
        self.fail = fail
        self.calls = 0
        self.last_usage = {}
        self.closed = False

    def generate_stream(self, messages):
        self.calls += 1
        try:
            yield StreamEvent("content", text=self.text)
            if self.fail:
                yield StreamEvent("error", text="connection lost", retryable=True)
            else:
                yield StreamEvent("finish", finish_reason="stop")
        finally:
            self.closed = True


def test_partial_stream_never_replays_published_sections():
    bp, pack = demo_pack("HOW")
    first = WRITER_DEMOS["HOW"].split("<<<SECTION:S2>>>")[0]
    client = FakeStream(first, fail=True)
    writer = TeachingWriterV3(lambda: client)
    emitted = []
    sections, trace = writer.write("q", bp, pack, OutputBudget(8000, 3, False, "test"),
        request_started=time.perf_counter(), on_section=emitted.append)
    assert trace["completion_status"] == "partial"
    assert client.calls == 1 and len(sections) == len(emitted) == 1 and client.closed


def test_prepublication_retry_is_bounded_and_can_complete():
    bp, pack = demo_pack("HOW")
    clients = [FakeStream("", fail=True), FakeStream(WRITER_DEMOS["HOW"])]
    source = iter(clients)
    writer = TeachingWriterV3(lambda: next(source))
    sections, trace = writer.write("q", bp, pack, OutputBudget(8000, 3, False, "test"), request_started=time.perf_counter())
    assert len(sections) == 3 and trace["stream_retry_count"] == 1 and trace["completion_status"] == "complete"


def test_request_deadline_propagates_and_resets():
    with request_deadline(5):
        assert 0 < bounded_timeout(120) <= 5
        with request_deadline(1):
            assert bounded_timeout(120) <= 1
        assert bounded_timeout(120) > 1
    assert bounded_timeout(120) == 120
    with request_deadline(started=time.perf_counter()-181):
        with pytest.raises(RequestDeadlineExceeded):
            bounded_timeout(120)


def test_v3_dispatch_and_full_result(monkeypatch):
    bp, _ = demo_pack("HOW")
    planner = SimpleNamespace(last_client=None, attempts=[], plan=lambda *a, **kw: bp)
    writer = TeachingWriterV3(lambda: FakeStream(WRITER_DEMOS["HOW"]))
    host = TeachingExplanationWorkflow(None, None, runtime_options=TeachingRuntimeOptions("v3"),
        v3_planner=planner, v3_writer=writer, capabilities=ModelCapabilities(131072, 32768))
    result = host.run(UserRequest("详细解释发布如何工作", AnswerOptions("detailed", "teach")), package())
    assert result.completion_status == "complete"
    assert result.stats["trace"]["blueprint"]["question_kind"] == "HOW"
    assert len(result.context_bundle.items) == 3


def test_shallow_depth_falls_back_without_v3_planning(monkeypatch):
    host = TeachingExplanationWorkflow(None, None, runtime_options=TeachingRuntimeOptions("v3"))
    result = SimpleNamespace(stats={})
    monkeypatch.setattr(host, "_run_single_stream", lambda *a, **kw: result)
    assert host.run(UserRequest("q", AnswerOptions("brief", "teach")), package()).stats["trace"]["fallback"] == "depth"


def test_unsupported_does_not_invoke_writer():
    def unsupported(*a, **kw):
        raise UnsupportedQuestionKind("WHAT", "纯定义")
    host = TeachingExplanationWorkflow(None, None, runtime_options=TeachingRuntimeOptions("v3"),
        v3_planner=SimpleNamespace(last_client=None, attempts=[], plan=unsupported), v3_writer=SimpleNamespace())
    result = host.run(UserRequest("详细解释 X 是什么", AnswerOptions("detailed", "teach")), package())
    assert result.completion_status == "failed" and result.stats["trace"]["question_kind"] == "UNSUPPORTED"


def test_real_planner_writer_pipeline_uses_two_calls_and_complete_sample():
    class Client:
        last_usage = {}
        last_finish_reason = "stop"
        max_tokens = 12000
        timeout_seconds = 120
        calls = 0

        def generate(self, messages):
            self.calls += 1
            assert "selected_depth" in messages[1].content
            return json.dumps(load_demo("HOW")["blueprint"], ensure_ascii=False)

    client = Client()
    stream = FakeStream(WRITER_DEMOS["HOW"])
    caps = ModelCapabilities(131072, 32768)
    host = TeachingExplanationWorkflow(None, None, runtime_options=TeachingRuntimeOptions("v3"),
        v3_planner=TeachingPlannerV3(lambda: client, caps, HeuristicTokenEstimator()),
        v3_writer=TeachingWriterV3(lambda: stream), capabilities=caps)
    result = host.run(UserRequest("详细解释发布如何工作", AnswerOptions("detailed", "teach")), package())
    assert result.completion_status == "complete"
    assert client.calls == stream.calls == 1
    assert client.timeout_seconds <= 120


def test_planner_structural_retry_and_insufficient_remaining_budget():
    class Client:
        last_usage = {}
        last_finish_reason = "stop"
        calls = 0

        def generate(self, messages):
            self.calls += 1
            return "{}" if self.calls == 1 else json.dumps(load_demo("HOW")["blueprint"], ensure_ascii=False)

    caps = ModelCapabilities(131072, 32768)
    client = Client()
    planner = TeachingPlannerV3(lambda: client, caps, HeuristicTokenEstimator())
    with request_deadline():
        assert planner.plan(UserRequest("q", AnswerOptions("detailed", "teach")), package(), depth="detailed").question_kind == "HOW"
    assert client.calls == 2
    client.calls = 0
    with request_deadline(80):
        with pytest.raises(PlannerFailure):
            planner.plan(UserRequest("q", AnswerOptions("detailed", "teach")), package(), depth="detailed")
    assert client.calls == 1


def test_configured_v3_deadline_is_reported_without_restarting_request():
    bp, _ = demo_pack('HOW')
    host = TeachingExplanationWorkflow(None, None, runtime_options=TeachingRuntimeOptions('v3'),
        v3_planner=SimpleNamespace(last_client=None, attempts=[], plan=lambda *a, **kw: bp),
        v3_writer=TeachingWriterV3(lambda: FakeStream(WRITER_DEMOS['HOW'])),
        capabilities=ModelCapabilities(131072, 32768))
    host.request_timeout_seconds = 300
    host.request_started_at = time.perf_counter() - 10
    result = host.run(UserRequest('how', AnswerOptions('detailed', 'teach')), package())
    assert result.completion_status == 'complete'
    assert result.stats['trace']['deadline_seconds'] == 300
    assert result.stats['trace']['total_elapsed_ms'] >= 10000
