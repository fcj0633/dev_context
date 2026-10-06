from copy import deepcopy
from types import SimpleNamespace
import json
import time
import pytest

from devcontext.answer_policy import INTENTS, resolve_policy
from devcontext.config import Settings
from devcontext.context.budget import ModelCapabilities
from devcontext.context.estimator import HeuristicTokenEstimator
from devcontext.deadline import request_deadline, remaining_seconds, RequestDeadlineExceeded
from devcontext.explanation.micro import MicroExplanationPlanner, parse_micro_plan
from devcontext.explanation.stream_writer import SingleStreamingTeachingWriter, SectionParser, validate_section
from devcontext.explanation.v3.demos import load_demo, WRITER_DEMOS
from devcontext.explanation.v3.evidence_organizer import build_writer_evidence_pack, resolve_section_dependencies
from devcontext.explanation.v3.planner import TeachingPlannerV3
from devcontext.explanation.v3.universal import example, parse_answer_blueprint
from devcontext.explanation.v3.writer import TeachingWriterV3
from devcontext.explanation.workflow import TeachingExplanationWorkflow
from devcontext.llm.client import StreamEvent
from devcontext.llm.factory import create_llm_client
from devcontext.models import ContextBundle, ContextItem, Citation
from devcontext.request import UserRequest, AnswerOptions

CAPS = ModelCapabilities(131072, 32768)

def package(demo, empty=False):
    evidence = [] if empty else demo["input"]["evidence"]
    items = [ContextItem(Citation(e["label"], "DOCUMENT", "fictional.md"), e["content"], i, "DOCUMENT", 1., i) for i, e in enumerate(evidence, 1)]
    return SimpleNamespace(original_query=demo["input"]["question"], evidence_workspace=None,
        context_bundle=ContextBundle("q", items, "", 0, 28000, False), retrieval_state="EMPTY" if empty else "READY",
        requirement_coverage=(), evidence_plan=SimpleNamespace(to_dict=lambda: {}), evidence_catalog=None)

class Client:
    model = "test"
    reasoning_effort = None
    last_finish_reason = "stop"
    last_usage = {}
    max_tokens = 12000
    timeout_seconds = 240
    calls = 0
    def __init__(self, response):
        self.response = response
    def generate(self, messages):
        self.calls += 1
        return json.dumps(self.response, ensure_ascii=False)
    def generate_stream(self, messages):
        self.calls += 1
        yield StreamEvent("content", text=self.response)
        yield StreamEvent("finish", finish_reason="stop")

def test_policy_defaults_and_explicit_priority(monkeypatch):
    monkeypatch.delenv("ANSWER_PROFILE", raising=False)
    monkeypatch.delenv("ANSWER_REASONING_EFFORT", raising=False)
    settings = Settings(_env_file=None)
    assert resolve_policy("q", settings).profile == "fast"
    assert resolve_policy("q", settings).reasoning_effort == "low"
    assert resolve_policy("q", settings, profile="full").reasoning_effort == "high"
    assert resolve_policy("q", settings).hard_timeout_seconds is None

def test_unsupported_effort_is_really_omitted(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    settings = Settings(_env_file=None)
    client = create_llm_client(settings, requested_reasoning_effort="medium")
    assert client.reasoning_effort is None
    assert client.requested_reasoning_effort == "medium"
    assert client.reasoning_omission_reason
    assert create_llm_client(settings, requested_reasoning_effort="low").reasoning_effort == "low"
    configured = settings.model_copy(update={"llm_supported_reasoning_efforts": "low,medium,high"})
    assert create_llm_client(configured, requested_reasoning_effort="medium").reasoning_effort == "medium"
    unknown = settings.model_copy(update={"deepseek_model": "unknown"})
    assert create_llm_client(unknown, requested_reasoning_effort="high").reasoning_effort is None

@pytest.mark.parametrize("profile", ["fast", "full"])
@pytest.mark.parametrize("kind", INTENTS)
def test_all_intents_execute_two_answer_calls(profile, kind):
    demo = load_demo(kind) if kind in {"WHY", "HOW"} else example(kind)
    policy = resolve_policy(demo["input"]["question"], Settings(_env_file=None), profile=profile)
    request = UserRequest(demo["input"]["question"], AnswerOptions(answer_mode="teach"), policy=policy)
    pkg = package(demo)
    if profile == "full":
        planner = Client(demo["blueprint"])
        writer = Client(WRITER_DEMOS[kind] if kind in {"WHY", "HOW"} else demo["body"])
        host = TeachingExplanationWorkflow(None, None, capabilities=CAPS,
            v3_planner=TeachingPlannerV3(lambda: planner, CAPS, HeuristicTokenEstimator()),
            v3_writer=TeachingWriterV3(lambda: writer, permissive=True))
    else:
        planner = Client({"direct_answer": "直接回答", "core_mental_model": "建立关系", "answer_depth": "brief", "question_kind": kind,
            "sections": [{"id": "S1", "title": "回答", "teaching_goal": "解释关系", "key_points": ["机制与条件"], "evidence_labels": [], "target_tokens": 100}]})
        writer = Client("<<<SECTION:S1>>>\n## 回答\n概念解释，无需引用。\n<<<END_SECTION:S1>>>")
        host = TeachingExplanationWorkflow(None, None, capabilities=CAPS,
            micro_planner=MicroExplanationPlanner(lambda: planner, CAPS), streaming_writer=SingleStreamingTeachingWriter(lambda: writer, permissive=True))
    result = host.run(request, pkg)
    assert result.completion_status == "complete", result.error
    assert planner.calls == writer.calls == 1
    assert result.explanation_plan.question_kind == kind

def test_empty_evidence_and_unavailable_labels_do_not_block_full():
    demo = example("WHAT")
    blueprint = parse_answer_blueprint(demo["blueprint"], allowed_labels=set(), expected_depth="standard")
    pack = build_writer_evidence_pack(blueprint, package(demo, empty=True), permissive=True)
    assert not pack.catalog and len(pack.sections) == 3
    warnings = []
    section = validate_section(pack.sections[0], "## 过期控制的是读取资格\n解释概念 [E99] [C1]", set(), permissive=True, warnings=warnings)
    assert not section.citations and "E99" not in section.markdown and len(warnings) == 2
    assert blueprint.warnings

@pytest.mark.parametrize("kind", ["WHAT", "COMPARE", "LOCATE"])
def test_new_complete_examples_match_pack_and_protocol(kind):
    demo = example(kind)
    blueprint = parse_answer_blueprint(demo["blueprint"], allowed_labels={"E1", "E2", "E3"}, expected_depth="detailed")
    pack = build_writer_evidence_pack(blueprint, package(demo), permissive=True)
    parser = SectionParser(s.id for s in pack.sections)
    parsed = list(parser.feed(demo["body"]))
    parser.finish()
    for section, (_, body) in zip(pack.sections, parsed, strict=True):
        validate_section(section, body, {e["label"] for e in pack.catalog}, permissive=True)

def test_explicit_deadline_only_and_partial_delivery():
    with request_deadline(None, started=time.perf_counter()-500):
        assert remaining_seconds() is None
    with request_deadline(1, started=time.perf_counter()-2):
        with pytest.raises(RequestDeadlineExceeded):
            remaining_seconds()
    demo = example("WHAT")
    bp = parse_answer_blueprint(demo["blueprint"], allowed_labels={"E1", "E2", "E3"}, expected_depth="detailed")
    pack = build_writer_evidence_pack(bp, package(demo), permissive=True)
    first = demo["body"].split("<<<SECTION:S2>>>")[0]
    writer_client = Client(first)
    writer = TeachingWriterV3(lambda: writer_client, permissive=True)
    from devcontext.explanation.budget import OutputBudget
    sections, trace = writer.write("q", bp, pack, OutputBudget(4000, 3, False, "test"), request_started=time.perf_counter())
    assert trace["completion_status"] == "partial" and len(sections) == 1 and writer_client.calls == 1

def test_cli_conflicts_rejected_before_clients(capsys):
    from devcontext.cli import main
    assert main(["ask", "q", "--profile", "fast", "--teaching-generation-mode", "v3"]) == 1
    assert main(["ask", "q", "--profile", "full", "--answer-mode", "legacy"]) == 1
    assert main(["ask", "q", "--reasoning-effort", "high", "--teaching-generation-mode", "multi_pass"]) == 1


def test_fast_combined_planning_and_one_semantic_check():
    from devcontext.agentic.fast import FastRetrievalPlanner, FastActionPlanner, FastCoverageChecker
    from devcontext.agentic.evidence_models import RequirementCoverage
    from devcontext.planning import EvidenceRequirement
    response = {"primary_intent": "WHY", "subjects": ["责任链"], "requirements": [{"target": "确认规则组织", "success_criteria": "找到调用与装配",
        "priority": "CORE", "temporal_scope": "CURRENT", "source_requirement": "ANY", "query": "责任链校验", "reason": "找装配"}]}
    client = Client(response)
    combined = FastRetrievalPlanner(lambda: client)
    plan = combined.plan("为何使用责任链")
    actions = FastActionPlanner(combined).plan_actions(plan.original_query, plan.requirements, round_index=0)
    assert client.calls == 1 and len(actions) == 1 and combined.primary_intent == "WHY"
    class Semantic:
        last_client = None
        calls = 0
        def check(self, requirements, view):
            self.calls += 1
            return tuple(RequirementCoverage(r.id, "PARTIAL", (1,), ("缺口",), "待补查", "llm") for r in requirements)
    semantic = Semantic()
    checker = FastCoverageChecker(semantic, combined)
    demo = example("WHAT")
    items = package(demo).context_bundle.items
    view = SimpleNamespace(round_index=0, items_for=lambda _: items)
    checker.check(plan.requirements, view)
    view.round_index = 1
    checker.check(plan.requirements, view)
    assert semantic.calls == 1
    # Clear subject match bypasses the LLM without claiming confirmed coverage.
    combined.subjects = ("到期",)
    view.round_index = 0
    statuses = FastCoverageChecker(semantic, combined).check(plan.requirements, view)
    assert statuses[0].state == "UNVERIFIED" and semantic.calls == 1


def test_empty_retrieval_reaches_answer_engine():
    from test_evidence_workflow import FakeController
    from devcontext.agentic.evidence_workflow import EvidenceDrivenWorkflow
    query = "当前注册入口在哪里？"
    policy = resolve_policy(query, Settings(_env_file=None))
    planner = Client({"direct_answer": "尚未找到入口", "core_mental_model": "查找入口", "answer_depth": "standard", "question_kind": "LOCATE",
        "sections": [{"id": "S1", "title": "查找建议", "teaching_goal": "说明检索止点", "key_points": ["按业务词找入口"], "evidence_labels": [], "target_tokens": 100}]})
    writer = Client("<<<SECTION:S1>>>\n## 查找建议\n当前未找到具体位置，可先按注册请求业务词检索入口。\n<<<END_SECTION:S1>>>")
    host = TeachingExplanationWorkflow(None, None, capabilities=CAPS,
        micro_planner=MicroExplanationPlanner(lambda: planner, CAPS), streaming_writer=SingleStreamingTeachingWriter(lambda: writer, permissive=True))
    def forbidden():
        raise AssertionError("old generator must not be created")
    workflow = EvidenceDrivenWorkflow(FakeController(), forbidden, answer_mode="teach", teaching_workflow=host, request_policy=policy)
    result = workflow.run(query, 12)
    assert result.trace.teaching["completion_status"] == "complete"
    assert result.trace.teaching["profile"] == "fast"
    assert result.trace.evidence_package_state == "EMPTY"
    assert planner.calls == writer.calls == 1


def test_link_and_checkpoint_dependencies_are_included():
    raw = example("WHAT")["blueprint"]
    raw["explanation_units"][1]["links"] = [{"from_unit": "U1", "relation": "DEPENDS_ON", "explanation": "以前一关系为前提"}]
    raw["scenario"]["checkpoints"] = [{"id": "K1", "spine_refs": ["U3"], "claim_ids": ["C3"]}]
    raw["answer_structure"][1]["checkpoint_ids"] = ["K1"]
    own, claims, *_ = resolve_section_dependencies(raw, raw["answer_structure"][1])
    assert own == ["C2"] and claims == ["C1", "C2", "C3"]


def test_required_evidence_is_not_silently_cropped():
    demo = example("WHAT")
    for evidence in demo["input"]["evidence"]:
        evidence["content"] += "示范材料背景。" * 1000
    pkg = package(demo)
    original = pkg.context_bundle.items[0].content
    bp = parse_answer_blueprint(demo["blueprint"], allowed_labels={"E1", "E2", "E3"}, expected_depth="detailed")
    from devcontext.explanation.v3.errors import EvidencePackUnavailable
    with pytest.raises(EvidencePackUnavailable, match="必要证据"):
        build_writer_evidence_pack(bp, pkg, max_chars=4500, permissive=True)
    assert pkg.context_bundle.items[0].content == original
