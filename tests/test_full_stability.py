from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from devcontext.answer_policy import resolve_policy
from devcontext.config import Settings
from devcontext.context.budget import ModelCapabilities
from devcontext.context.estimator import HeuristicTokenEstimator
from devcontext.deadline import request_deadline
from devcontext.explanation.v3.demos import load_demo, WRITER_DEMOS
from devcontext.explanation.v3.evidence_organizer import demo_pack
from devcontext.explanation.v3.errors import PlannerFailure
from devcontext.explanation.v3.errors import EvidencePackUnavailable
from devcontext.explanation.v3.fallback import direct_messages
from devcontext.explanation.v3.planner import TeachingPlannerV3
from devcontext.explanation.v3.universal import parse_answer_blueprint
from devcontext.explanation.v3.workflow import run_v3
from devcontext.explanation.v3.writer import TeachingWriterV3
from devcontext.llm.client import StreamEvent
from devcontext.llm.errors import http_failure
from devcontext.request import UserRequest, AnswerOptions

CAPS = ModelCapabilities(131072, 32768)
EST = HeuristicTokenEstimator()


def parse(raw):
    return parse_answer_blueprint(raw, allowed_labels={"E1", "E2", "E3"})


def test_actual_concurrent_order_failure_disables_only_scenario():
    raw = json.loads((Path(__file__).parent / "fixtures/full-concurrent-order-rejected.json").read_text(encoding="utf-8"))
    labels = {e for c in raw["claims"] for e in c["evidence_labels"]}
    original = deepcopy(raw)
    bp = parse_answer_blueprint(raw, allowed_labels=labels)
    assert raw == original
    assert bp.data["scenario"]["kind"] == "NONE"
    assert bp.data["how_spine"] == original["how_spine"]
    assert all(s["checkpoint_ids"] == [] for s in bp.sections)
    assert any("K4" in w and "FAILURE_PARENT" in w for w in bp.warnings)


def test_fences_and_unique_noncontinuous_ids_keep_original_identity():
    raw = load_demo("HOW")["blueprint"]
    raw["claims"][0]["id"] = "C99"
    encoded = json.dumps(raw).replace('"C1"', '"C99"')
    bp = parse("```json\n" + encoded + "\n```")
    assert bp.data["claims"][0]["id"] == "C99"
    assert "JSON_FENCE_REMOVED" in bp.warnings
    raw["claims"][1]["id"] = "C99"
    with pytest.raises(PlannerFailure) as exc:
        parse(raw)
    assert exc.value.issues[0]["code"] == "INVALID_ID"


def test_independent_core_reference_errors_are_reported_together():
    raw = load_demo("HOW")["blueprint"]
    raw["answer_structure"][0]["goal_ids"] = ["G99"]
    raw["answer_structure"][1]["may_reference"] = ["C99"]
    with pytest.raises(PlannerFailure) as exc:
        parse(raw)
    assert len(exc.value.issues) == 2
    assert all({"code", "path", "id", "expected", "actual"} <= i.keys() for i in exc.value.issues)


@pytest.mark.parametrize("mutate", [
    lambda d: d["scenario"]["checkpoints"][-1].update(parent_checkpoint_id=None),
    lambda d: d["scenario"]["checkpoints"][0].update(branch="WORLD_A"),
    lambda d: d["scenario"]["checkpoints"][0].update(claim_ids=["C99"]),
    lambda d: [s.update(checkpoint_ids=[]) for s in d["answer_structure"]],
])
def test_optional_scenario_errors_do_not_discard_core(mutate):
    raw = load_demo("HOW")["blueprint"]
    mutate(raw)
    bp = parse(raw)
    assert bp.data["scenario"]["kind"] == "NONE"
    assert len(bp.data["how_spine"]) == len(raw["how_spine"])


class Client:
    model = "test-model"
    reasoning_effort = "high"
    max_tokens = 32768
    timeout_seconds = 600
    last_finish_reason = "stop"
    last_usage = {}
    def __init__(self, outputs, stream=False):
        self.outputs = list(outputs)
        self.messages = []
        self.stream = stream
    def generate(self, messages):
        self.messages.append(messages)
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    def generate_stream(self, messages):
        self.messages.append(messages)
        value = self.outputs.pop(0)
        if isinstance(value, str):
            yield StreamEvent("content", text=value)
            yield StreamEvent("finish", finish_reason="stop")
        else:
            yield from value


def setup(planner_outputs, writer_outputs):
    pc, wc = Client(planner_outputs), Client(writer_outputs, True)
    host = SimpleNamespace(v3_planner=TeachingPlannerV3(lambda: pc, CAPS, EST),
                           v3_writer=TeachingWriterV3(lambda: wc, permissive=True), capabilities=CAPS, estimator=EST)
    _, pack = demo_pack("HOW")
    package = SimpleNamespace(original_query="如何发布内容", context_bundle=deepcopy(pack.bundle), evidence_workspace=None, requirement_coverage=(),
                              evidence_plan=SimpleNamespace(to_dict=lambda: {}), retrieval_state="READY", evidence_catalog=None)
    policy = resolve_policy("如何发布内容", Settings(_env_file=None), profile="full")
    request = UserRequest("如何发布内容", AnswerOptions(answer_mode="teach"), policy=policy)
    return host, request, package, pc, wc


def valid():
    return json.dumps(load_demo("HOW")["blueprint"], ensure_ascii=False)


def test_planner_correction_includes_rejected_output_and_saves_both_attempts():
    host, req, package, pc, wc = setup(["{}", valid()], [WRITER_DEMOS["HOW"]])
    result = run_v3(host, req, package)
    assert result.completion_status == "complete"
    assert result.stats["trace"]["delivery_path"] == "full_repaired"
    assert any(m.role == "assistant" and m.content == "{}" for m in pc.messages[1])
    assert [a["response"] for a in host.v3_planner.attempts] == ["{}", valid()]


def test_two_bad_plans_use_one_direct_call_with_original_evidence():
    host, req, package, pc, wc = setup(["bad json", "{}"], ["直接回答。[E1] [E999]"])
    published = []
    result = run_v3(host, req, package, on_section=published.append)
    trace = result.stats["trace"]
    assert result.completion_status == "complete"
    assert trace["delivery_path"] == "full_direct_fallback"
    assert trace["sections_planned"] == trace["sections_emitted"] == 1
    assert len(pc.messages) == 2 and len(wc.messages) == 1 and len(published) == 1
    assert "E999" not in result.answer.answer
    payload = json.loads(wc.messages[0][-1].content)
    assert payload["evidence"][0]["label"] == package.context_bundle.items[0].citation.label
    assert wc.reasoning_effort == "high"


def test_writer_retries_with_specific_protocol_then_falls_back_once():
    host, req, package, pc, wc = setup(["{}", valid()], ["wrong protocol", "wrong again", "普通正文。[E1]"])
    result = run_v3(host, req, package)
    assert result.completion_status == "complete"
    assert result.stats["trace"]["delivery_path"] == "full_direct_fallback"
    assert len(wc.messages) == 3
    assert len(pc.messages) + len(wc.messages) == 5
    assert any(m.role == "assistant" and m.content == "wrong protocol" for m in wc.messages[1])
    assert "<<<END_SECTION:S1>>>" in wc.messages[1][-1].content


def test_partial_publication_does_not_retry_or_fallback():
    first = WRITER_DEMOS["HOW"].split("<<<SECTION:S2>>>")[0]
    host, req, package, pc, wc = setup([valid()], [[StreamEvent("content", text=first), StreamEvent("error", text="disconnect")]])
    published = []
    result = run_v3(host, req, package, on_section=published.append)
    assert result.completion_status == "partial"
    assert len(published) == len(wc.messages) == 1


@pytest.mark.parametrize("reason,body,status", [("length", "未完成正文", "partial"), ("length", "", "failed"), ("stop", "正文", "complete")])
def test_direct_finish_semantics(reason, body, status):
    events = [StreamEvent("content", text=body), StreamEvent("finish", finish_reason=reason)]
    host, req, package, _, wc = setup(["{}", "{}"], [events])
    result = run_v3(host, req, package)
    assert result.completion_status == status
    assert len(wc.messages) == 1


def test_configuration_failure_and_hard_deadline_stop_calls():
    host, req, package, pc, wc = setup([http_failure("denied", 401)], [])
    result = run_v3(host, req, package)
    assert result.completion_status == "failed"
    assert len(pc.messages) == 1 and wc.messages == []
    with request_deadline(-1):
        result = run_v3(host, req, package)
    assert result.completion_status == "failed"
    assert len(pc.messages) == 1


def test_callback_failure_never_replays_or_falls_back():
    host, req, package, _, wc = setup([valid()], [WRITER_DEMOS["HOW"]])
    callbacks = []
    def broken(section):
        callbacks.append(section)
        raise RuntimeError("UI unavailable")
    result = run_v3(host, req, package, on_section=broken)
    assert result.completion_status == "failed"
    assert len(callbacks) == len(wc.messages) == 1


def test_direct_input_omits_whole_oversized_evidence_blocks():
    host, req, package, _, _ = setup([], [])
    package.context_bundle.items[0].content = "X" * 900000
    messages, labels, omitted, _ = direct_messages(req, package, None, CAPS, EST)
    assert "E1" in omitted and "E1" not in labels
    assert not any("X" * 100 in m.content for m in messages)


def test_organizer_failure_uses_existing_bundle_without_replanning(monkeypatch):
    host, req, package, pc, wc = setup([valid()], ["兜底正文。[E1]"])
    def broken(*args, **kwargs):
        raise EvidencePackUnavailable("necessary pack exceeds window")
    monkeypatch.setattr("devcontext.explanation.v3.workflow.build_writer_evidence_pack", broken)
    result = run_v3(host, req, package)
    assert result.completion_status == "complete"
    assert len(pc.messages) == len(wc.messages) == 1
    assert result.stats["trace"]["fallback_reason"] == "necessary pack exceeds window"


def test_planner_correction_over_window_skips_second_call():
    host, req, package, pc, wc = setup([" " * 800000 + "{}"], ["直接回答"])
    class Estimate:
        def estimate(self, text):
            return max(1, len(text)//100)
    host.estimator = host.v3_planner.estimator = Estimate()
    host.capabilities = host.v3_planner.capabilities = ModelCapabilities(35000, 32768)
    result = run_v3(host, req, package)
    assert result.completion_status == "complete"
    assert len(pc.messages) == len(wc.messages) == 1
    assert host.v3_planner.attempts[0]["retry_skipped"] == "correction exceeds model window"


def test_writer_correction_over_window_skips_second_call():
    import time
    from devcontext.explanation.budget import OutputBudget
    bp, pack = demo_pack("HOW")
    client = Client(["wrong marker"], True)
    writer = TeachingWriterV3(lambda: client, permissive=True)
    writer.capabilities, writer.estimator = ModelCapabilities(10, 10), EST
    sections, trace = writer.write("q", bp, pack, OutputBudget(10, 3, False, "test"), request_started=time.perf_counter())
    assert sections == () and trace["completion_status"] == "failed"
    assert len(client.messages) == 1
    assert trace["attempts"][0]["retry_skipped"] == "correction exceeds model window"


@pytest.mark.parametrize("field", ["claim_type", "status", "owner_section"])
def test_invalid_core_field_types_are_repairable_errors(field):
    raw = load_demo("HOW")["blueprint"]
    raw["claims"][0][field] = []
    with pytest.raises(PlannerFailure):
        parse(raw)


def test_unsupported_scenario_branch_type_disables_scenario():
    raw = load_demo("HOW")["blueprint"]
    raw["scenario"]["checkpoints"][0]["branch"] = []
    assert parse(raw).data["scenario"]["kind"] == "NONE"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_fatal_request_scope_blocks_later_transports_and_resets(status):
    from devcontext.llm.chat_completions import ChatCompletionsLLMClient
    from devcontext.llm.errors import fatal_request_scope, LLMRequestError
    client = ChatCompletionsLLMClient("test-key")
    calls = []
    def transport(messages):
        calls.append(messages)
        raise http_failure("fatal service response", status)
    client._generate = transport
    with fatal_request_scope():
        for _ in range(3):
            with pytest.raises(LLMRequestError):
                client.generate([])
    assert len(calls) == 1
    with fatal_request_scope():
        with pytest.raises(LLMRequestError):
            client.generate([])
    assert len(calls) == 2


def test_transient_service_error_does_not_latch_request():
    from devcontext.llm.errors import fatal_request_scope, check_fatal_request
    with fatal_request_scope():
        exc = http_failure("try again", 503)
        assert exc.retryable
        check_fatal_request()
