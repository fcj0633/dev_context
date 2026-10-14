from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from devcontext.context.relations import EvidenceSymbol, EvidenceRelation, RelationProbe, ConfirmedPath
from devcontext.observability.diagnostics import (capture_diagnostics, mark, record_package,
    begin_model, model_content, end_model, restore_package, snapshot_package, replay_answer, clean)
from devcontext.observability.recorder import llm_stage
from devcontext.observability.trace import PerfRecorder, LLMCallTrace
from devcontext.evaluation.answer_quality_runner import _usage_totals
from devcontext.request import UserRequest, AnswerOptions
from devcontext.answer_policy import resolve_policy
from devcontext.config import Settings
from devcontext.llm.client import LLMMessage, StreamEvent
from devcontext.llm.deepseek import DeepSeekLLMClient
from test_teaching_workflow import make_package, FakeTeachingWorkflow, workflow


def request_for(package):
    return UserRequest(package.original_query, AnswerOptions("teach"),
                       policy=resolve_policy(package.original_query, Settings(_env_file=None)))


def test_disabled_hooks_do_not_serialize_inputs():
    class Explosive:
        def to_dict(self):
            raise AssertionError("disabled diagnostic tried to capture content")
    assert begin_model(Explosive(), [Explosive()]) is None
    record_package(Explosive(), Explosive())
    mark("writer_started")
    model_content(None, "text")
    end_model(None, Explosive())


def test_snapshot_round_trip_and_order_independent_labels():
    package = make_package()
    request = request_for(package)
    raw = snapshot_package(request, package)
    raw["workspace"]["refs"].reverse()
    restored_request, restored = restore_package(json.loads(json.dumps(raw)))
    assert restored_request == request
    assert restored.context_bundle.to_dict() == package.context_bundle.to_dict()
    assert restored.evidence_catalog.to_dict() == package.evidence_catalog.to_dict()
    assert restored.evidence_workspace.frozen
    assert restored.evidence_workspace.get(1).evidence_id == "E1"
    assert restored.evidence_workspace.get(2).content == "body 2"
    assert restored.requirement_coverage == package.requirement_coverage


def test_structure_provenance_path_probe_and_round_ownership_survive_json():
    package = make_package()
    ws = package.evidence_workspace
    ws._frozen = False
    # Both endpoints must be scoped to the same requirement.
    ws._refs[2] = replace(ws._refs[2], requirement_ids=("ER2", "ER1"))
    ws._by_requirement["ER1"].append(2)
    ws._chunk_ownership_round["ER1", 2] = 1
    keys = ("M:Service.a()", "M:Service.b()")
    ws.add_symbols((EvidenceSymbol(keys[0], "METHOD", 1), EvidenceSymbol(keys[1], "METHOD", 2)), "ER1", 1)
    edge = EvidenceRelation("repo", *keys, "CALLS", "AST_EXACT", 3, 1, 1, 2)
    ws.add_relations((edge,), "ER1", 1, "graph", "call1")
    ws.add_probe(RelationProbe("ER1", (0,), keys, ("CALLS",), ("OUTGOING",), 1, "COMPLETED", relation_ids=(edge.identity,)))
    ws.add_path(ConfirmedPath("ER1", 0, keys, (edge.identity,), ("OUTGOING",), 1))
    ws.freeze()
    _, restored = restore_package(json.loads(json.dumps(snapshot_package(request_for(package), package))))
    assert restored.evidence_workspace.structural_metadata() == ws.structural_metadata()
    assert restored.evidence_workspace._chunk_ownership_round == ws._chunk_ownership_round
    assert not restored.evidence_workspace.relations_for("ER1", 0)
    assert restored.evidence_workspace.relations_for("ER1", 1) == (edge,)


@pytest.mark.parametrize("state", ["READY", "PARTIAL", "EMPTY", "RETRIEVAL_FAILED"])
def test_evidence_states_are_preserved(state):
    package = replace(make_package(), retrieval_state=state)
    _, restored = restore_package(snapshot_package(request_for(package), package))
    assert restored.retrieval_state == state


def test_empty_workspace_and_legacy_without_workspace():
    from devcontext.context import EvidenceWorkspace
    package = make_package()
    ws = EvidenceWorkspace(package.original_query)
    empty = replace(package, evidence_workspace=ws, evidence_catalog=ws.freeze(), retrieval_state="EMPTY")
    _, restored = restore_package(snapshot_package(request_for(empty), empty))
    assert len(restored.evidence_workspace) == 0
    legacy = replace(package, evidence_workspace=None, evidence_catalog=None)
    _, restored = restore_package(snapshot_package(request_for(legacy), legacy))
    assert restored.evidence_workspace is None
    assert restored.context_bundle.to_dict() == legacy.context_bundle.to_dict()


def test_replay_uses_only_injected_answer_workflow(tmp_path):
    package = make_package()
    with capture_diagnostics() as diagnostic:
        record_package(request_for(package), package)
        diagnostic.save(tmp_path / "sample.json")
    class AnswerOnly:
        def run(self, request, evidence, on_section=None):
            assert evidence.evidence_workspace.get(1).content == "body 1"
            return request.original_query, evidence.evidence_catalog.labels()
    assert replay_answer(tmp_path / "sample.json", AnswerOnly()) == (package.original_query, ("E1", "E2"))
    raw = diagnostic.to_dict()
    raw["schema_version"] = 99
    (tmp_path / "bad.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        replay_answer(tmp_path / "bad.json", AnswerOnly())


def test_exact_model_inputs_attempts_timeline_and_redaction(tmp_path):
    ticks = iter([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11])
    client = SimpleNamespace(model="test", max_tokens=123, last_finish_reason="stop", last_usage={"prompt_tokens": 2})
    secret = "secret-key-value"
    with capture_diagnostics(secrets=(secret,), clock=lambda: next(ticks)) as diagnostic:
        mark("retrieval_started")
        with llm_stage("teaching_draft"):
            a = begin_model(client, [LLMMessage("user", "body " + secret)], True)
            model_content(a, "<<<SECTION:S1>>>\n## Heading\n")
            assert diagnostic.timeline["provider_first_body"] is None
            model_content(a, "Explanation [E1]")
            end_model(a, client, "partial " + secret)
        diagnostic.finish(error="timeout " + secret)
        diagnostic.save(tmp_path / "sample.json")
    data = json.loads((tmp_path / "sample.json").read_text(encoding="utf-8"))
    assert secret not in json.dumps(data)
    assert data["model_attempts"][0]["parameters"]["max_tokens"] == 123
    assert data["timeline"]["provider_first_body"] > data["timeline"]["writer_started"]
    assert data["timeline"]["first_published"] is None
    assert data["timeline"]["browser_first_visible"] is None
    assert data["outcome"]["generation_status"] == "failed"


def test_real_client_generate_capture_is_opt_in(monkeypatch):
    client = DeepSeekLLMClient(api_key="test-secret")
    monkeypatch.setattr(client, "_generate", lambda messages: "answer")
    messages = [LLMMessage("user", "same request")]
    assert client.generate(messages) == "answer"
    with capture_diagnostics() as diagnostic, llm_stage("explanation_planning"):
        assert client.generate(messages) == "answer"
    assert diagnostic.attempts[0]["messages"] == [m.to_dict() for m in messages]
    assert diagnostic.attempts[0]["finished_ms"] is not None


def test_real_stream_records_body_and_interruption(monkeypatch):
    import devcontext.llm.streaming as streaming
    payload = 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    monkeypatch.setattr(streaming, "curl_stream", lambda client, messages: (part for part in [payload.encode()]))
    client = DeepSeekLLMClient(api_key="test-secret")
    with capture_diagnostics() as diagnostic, llm_stage("teaching_draft"):
        iterator = client.generate_stream([LLMMessage("user", "evidence")])
        assert next(iterator).text == "hello"
        iterator.close()
    assert diagnostic.timeline["provider_first_body"] is not None
    assert diagnostic.attempts[0]["error"] == "stream consumer stopped early"


def test_workflow_snapshot_captured_before_answer_failure():
    package = make_package()
    class Broken(FakeTeachingWorkflow):
        def run(self, request, package):
            raise TimeoutError("writer timeout")
    with capture_diagnostics() as diagnostic:
        with pytest.raises(TimeoutError):
            workflow("teach", package, Broken(package)).run(package.original_query, 12)
        diagnostic.finish(error="writer timeout")
    assert diagnostic.retrieval["workspace"]["refs"][0]["content"] == "body 1"
    assert diagnostic.outcome["generation_status"] == "failed"


def test_unknown_usage_is_not_zero_or_partial_total():
    recorder = PerfRecorder(llm_calls=[LLMCallTrace(1, "writer", 1, input_tokens=10, output_tokens=20), LLMCallTrace(2, "writer", 1)])
    totals = recorder.llm_totals()
    assert totals["input_tokens"] is None and totals["output_tokens"] is None
    assert totals["input_tokens_reporting"] == {"total": None, "known_total": 10, "reported_count": 1, "missing_count": 1}
    stages = [{"model": "test", "latency_ms": 1, "input_tokens": 10}, {"model": "test", "latency_ms": 1}]
    assert _usage_totals(stages)["input_tokens"] is None
    assert PerfRecorder().llm_totals()["input_tokens"] == 0


def test_snapshot_rejects_duplicate_or_mismatched_labels():
    package = make_package()
    raw = snapshot_package(request_for(package), package)
    raw["workspace"]["refs"][1]["evidence_id"] = "E1"
    with pytest.raises(ValueError):
        restore_package(raw)


def test_nested_context_scope_does_not_leak_and_scrubs_urls():
    with capture_diagnostics() as outer:
        mark("writer_started")
        with capture_diagnostics() as inner:
            mark("first_published")
        assert inner.timeline["writer_started"] is None
        assert outer.timeline["first_published"] is None
    assert begin_model(None, []) is None
    assert "password" not in clean({"nested": ["postgresql://user:password@localhost/db"]})["nested"][0]


def test_partial_markers_do_not_count_as_body_and_retries_are_separate():
    client = SimpleNamespace(model="test", last_finish_reason="stop", last_usage={})
    with capture_diagnostics() as diagnostic, llm_stage("teaching_draft"):
        first = begin_model(client, [LLMMessage("user", "first")], True)
        for part in ["<", "<<SECTION:S", "1>>>\n#", "# heading\n"]:
            model_content(first, part)
            assert diagnostic.timeline["provider_first_body"] is None
        end_model(first, client, "retryable timeout")
        second = begin_model(client, [LLMMessage("user", "second")], True)
        model_content(second, "<<<SECTION:S1>>>\n## heading\nbody")
        end_model(second, client)
    assert len(diagnostic.attempts) == 2
    assert diagnostic.timeline["provider_first_body"] is not None
    assert diagnostic.attempts[0]["messages"] != diagnostic.attempts[1]["messages"]


def test_cancellation_and_unexpected_failure_keep_progress():
    from devcontext.observability.diagnostics import record_progress
    class RequestCancelled(BaseException):
        pass
    package = make_package()
    with pytest.raises(RequestCancelled):
        with capture_diagnostics() as diagnostic:
            record_progress(package.evidence_plan, package.evidence_workspace, package.requirement_coverage)
            raise RequestCancelled()
    assert diagnostic.outcome["generation_status"] == "cancelled"
    assert diagnostic.retrieval_progress["refs"][0]["content"] == "body 1"
    assert diagnostic.timeline["request_finished"] is not None


def test_catalog_without_workspace_survives_replay():
    package = replace(make_package(), evidence_workspace=None)
    _, restored = restore_package(snapshot_package(request_for(package), package))
    assert restored.evidence_catalog.to_dict() == package.evidence_catalog.to_dict()


def test_quality_summary_keeps_partial_subtotals_without_type_error():
    from devcontext.evaluation.answer_quality_runner import _arm_metrics
    checks = {"passed": True, "in_target_range": True, "repeated_conclusion_prefix": False, "duplicate_headings": []}
    first = {"error": None, "checks": checks, "zero_valid_citation": False, "invalid_citations": [],
        "usage": _usage_totals([{"model": "test", "latency_ms": 1, "input_tokens": 10, "output_tokens": 20}])}
    second = {**first, "usage": _usage_totals([
        {"model": "test", "latency_ms": 1, "input_tokens": 3, "output_tokens": 4},
        {"model": "test", "latency_ms": 1}])}
    summary = _arm_metrics([{"arms": {"test": first}}, {"arms": {"test": second}}], "test")
    assert summary["input_tokens"] is None
    assert summary["input_tokens_reporting"]["known_total"] == 13
    assert summary["output_tokens_reporting"]["known_total"] == 24


def test_partial_outcome_distinguishes_generation_from_evidence():
    result = SimpleNamespace(trace=SimpleNamespace(teaching={"completion_status": "partial", "error": "disconnect"},
        evidence_package_state="READY"), answer_result=SimpleNamespace(answer="already published [E1]", used_citations=["E1"]),
        context_bundle=make_package().context_bundle)
    with capture_diagnostics() as diagnostic:
        diagnostic.finish(result)
    assert diagnostic.outcome["generation_status"] == "partial"
    assert diagnostic.outcome["evidence_status"] == "READY"
    assert diagnostic.outcome["answer"] == "already published [E1]"
