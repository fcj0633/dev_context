from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from devcontext.answer_policy import resolve_policy
from devcontext.cli import main, _profile_answer_factory
from devcontext.config import Settings
from devcontext.explanation.v3.demos import load_demo
from devcontext.explanation.v3.evidence_organizer import demo_pack, build_writer_evidence_pack, reconcile_evidence
from devcontext.explanation.v3.errors import PlannerFailure
from devcontext.explanation.v3.prompts import writer_messages
from devcontext.explanation.v3.universal import example, parse_answer_blueprint


def parse(raw):
    return parse_answer_blueprint(raw, allowed_labels={"E1", "E2", "E3"})


@pytest.mark.parametrize("kind", ["WHAT", "WHY", "HOW"])
def test_full_demonstration_is_the_complete_executable_writer_input(kind):
    bp, pack = demo_pack(kind)
    messages = writer_messages("真实问题", bp, pack, universal=True)
    demo = json.loads(messages[1].content.split("：", 1)[1])
    assert {"reader_assumption", "learning_goal", "goal_capabilities", "comprehension_checks", "mental_model", "scenario_setup", "section_contracts", "catalog"} <= demo.keys()
    assert "answer_depth" not in demo
    assert "示范说明" in messages[-1].content
    if kind == "HOW":
        assert "失败从已建立状态分叉" in messages[0].content
    if kind == "WHY":
        assert "同起点比较两世界" in messages[0].content


@pytest.mark.parametrize("mutation,error", [
    (lambda d: d["answer_structure"][0].update(goal_ids=[]), "goal coverage"),
    (lambda d: [s.update(spine_refs=[]) for s in d["answer_structure"]], "spine coverage"),
    (lambda d: [s.update(checkpoint_ids=[]) for s in d["answer_structure"]], "checkpoint coverage"),
    (lambda d: d["answer_structure"][0]["learning_delta"].update(after=d["answer_structure"][0]["learning_delta"]["before"]), "delta must change"),
    (lambda d: d["claims"][0].update(owner_section="S99"), "valid owner"),
    (lambda d: d["how_spine_tail"]["established_guarantee_claim_ids"].append("C4"), "unknown cannot"),
    (lambda d: d["how_spine"][-1].update(links=[]), "branch origin"),
])
def test_full_rejects_broken_teaching_dependencies(mutation, error):
    raw = load_demo("HOW")["blueprint"]
    # Isolate a goal for which only the first chapter is responsible.
    if error == "goal coverage":
        raw["goal_capabilities"].append({"id": "G6", "applicable": True, "capability": "理解首节关系", "reason": None})
        raw["answer_structure"][0]["goal_ids"].append("G6")
    mutation(raw)
    with pytest.raises(PlannerFailure, match=error):
        parse(raw)


def test_missing_evidence_reduces_guarantee_without_mutating_original():
    bp, pack = demo_pack("HOW")
    bundle = deepcopy(pack.bundle)
    bundle.items = [i for i in bundle.items if i.citation.label != "E1"]
    package = SimpleNamespace(evidence_workspace=None, context_bundle=bundle)
    reduced = reconcile_evidence(bp, package)
    assert reduced.data["claims"][0]["status"] == "UNKNOWN"
    assert bp.data["claims"][0]["status"] == "CONFIRMED"
    assert "C1" not in reduced.data["how_spine_tail"]["established_guarantee_claim_ids"]
    assert reduced.warnings


def test_later_chapters_do_not_receive_every_previous_claim():
    bp, pack = demo_pack("WHY")
    for section in pack.sections:
        assert set(section.contract["prior_context_claim_ids"]) <= {c["id"] for c in section.contract["may_reference"]}


def test_full_timeout_and_budget_use_profile_specific_values(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
    settings = Settings(_env_file=None, answer_profile="full")
    planner = _profile_answer_factory(settings, planner=True)()
    writer = _profile_answer_factory(settings)()
    assert (planner.timeout_seconds, writer.timeout_seconds) == (300, 600)
    assert planner.reasoning_effort == writer.reasoning_effort == "high"
    assert planner.max_tokens == writer.max_tokens == 32768
    assert "answer_depth" not in resolve_policy("简要解释", settings).to_dict()


def test_removed_depth_is_rejected_before_settings_or_retrieval(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["ask", "q", "--depth", "detailed"])
    assert exc.value.code == 2
    assert "已移除" in capsys.readouterr().err


def test_historical_depth_does_not_change_executable_blueprint():
    raw = example("WHAT")["blueprint"]
    current = parse(raw)
    raw["answer_depth"] = "deep"
    assert parse(raw).data == current.data
