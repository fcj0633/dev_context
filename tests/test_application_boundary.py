"""Application contracts: no transport, network, or real model required."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import ast

import pytest

from devcontext.application import AskRequest, AskService, PlanRequest, WorkflowFactory, RetrievalOverrides
from devcontext.cli import _parser, ask_request_from_cli
from devcontext.config import Settings
from devcontext.execution import CancellationToken, RequestCancelled, check_cancelled, publish_evidence
from devcontext.observability.diagnostics import capture_diagnostics, record_package, replay_answer
from test_teaching_workflow import make_package


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected component construction")


@pytest.mark.parametrize("overrides", [
    {"query": " "}, {"top_k": 0}, {"top_k": True}, {"max_chars": -1},
    {"legacy_max_chars": 0}, {"hard_timeout": float("nan")},
    {"hard_timeout": float("inf")}, {"hard_timeout": 0},
    {"answer_mode": "invalid"}, {"teaching_generation_mode": "invalid"},
    {"profile": ""}, {"reasoning_effort": "invalid"},
    {"profile": "full", "teaching_generation_mode": "single_stream"},
    {"profile": "fast", "answer_mode": "explain"}, {"plan_only": True},
])
def test_invalid_requests_fail_before_assembly(overrides):
    service = AskService(Settings(_env_file=None), factory_builder=forbidden)
    with pytest.raises(ValueError):
        service.ask(replace(AskRequest("q"), **overrides))


@pytest.mark.parametrize("argv,expected", [
    ([], ("fast", "teach", 12, 10000)),
    (["--profile", "full"], ("full", "teach", 12, 10000)),
    (["--no-plan"], (None, "legacy", 5, 6000)),
    (["--answer-mode", "legacy"], (None, "legacy", 12, 10000)),
    (["--answer-mode", "explain"], (None, "explain", 12, 10000)),
    (["--teaching-generation-mode", "multi_pass"], (None, "teach", 12, 10000)),
])
def test_cli_and_direct_requests_use_one_normalizer(argv, expected):
    service = AskService(Settings(_env_file=None, answer_profile="fast", answer_reasoning_effort=None))
    request = ask_request_from_cli(_parser().parse_args(["ask", "q", *argv]))
    prepared = service.prepare(request)
    direct = service.prepare(AskRequest(**{field: getattr(request, field) for field in request.__dataclass_fields__}))
    assert prepared == direct
    assert (prepared.policy.profile if prepared.policy else None, prepared.answer_mode,
            prepared.top_k, prepared.max_chars) == expected


@pytest.mark.parametrize("format", ["legacy", "evidence"])
def test_plan_constructs_only_requested_planner(format):
    calls = []
    class Factory:
        def __init__(self, **hooks):
            pass
        def build_question_planner(self, settings):
            calls.append("legacy")
            return SimpleNamespace(plan=lambda q: q)
        def build_evidence_planner(self, settings):
            calls.append("evidence")
            return SimpleNamespace(plan=lambda q: q)
        build_answer_workflow = forbidden
    assert AskService(Settings(_env_file=None), factory_builder=Factory).plan(PlanRequest("q", format)) == "q"
    assert calls == [format]


def test_prepared_execution_isolated_and_cancelled_before_factory():
    configs = []
    def build(settings, *args):
        configs.append(settings)
        settings.answer_profile = "full"
        return SimpleNamespace(run=lambda *args, **kwargs: "answer")
    service = AskService(Settings(_env_file=None), workflow_factory=build)
    prepared = service.prepare(AskRequest("q", profile="fast"))
    assert service.execute(prepared) == service.execute(prepared) == "answer"
    assert configs[0] is not configs[1]
    assert prepared.settings.answer_profile == "fast"
    token = CancellationToken()
    token.cancel()
    with pytest.raises(RequestCancelled):
        service.execute(prepared, cancellation=token)
    assert len(configs) == 2
    check_cancelled()


def test_concurrent_cancellation_evidence_and_configuration_do_not_cross_requests():
    barrier = Barrier(2)
    tokens = {p: CancellationToken() for p in ("fast", "full")}
    evidence = {p: [] for p in tokens}
    def build(settings, *args):
        profile = settings.answer_profile
        def run(*args, **kwargs):
            barrier.wait(timeout=5)
            if profile == "fast":
                tokens[profile].cancel()
            barrier.wait(timeout=5)
            publish_evidence(profile)
            return profile
        return SimpleNamespace(run=run)
    service = AskService(Settings(_env_file=None), workflow_factory=build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {p: pool.submit(service.ask, AskRequest(p, profile=p),
            cancellation=tokens[p], on_evidence=evidence[p].append) for p in tokens}
        with pytest.raises(RequestCancelled):
            jobs["fast"].result(timeout=10)
        assert jobs["full"].result(timeout=10) == "full"
    assert evidence == {"fast": [], "full": ["full"]}
    publish_evidence("outside")
    assert evidence["full"] == ["full"]


@pytest.mark.parametrize("error", [RuntimeError("model failed"), TimeoutError("deadline"), RequestCancelled()])
def test_failure_cleans_execution_scope_and_keeps_diagnostics(error):
    from devcontext.request import AnswerOptions, UserRequest
    def build(*args):
        def run(*args, **kwargs):
            package = make_package()
            record_package(UserRequest(package.original_query, AnswerOptions("teach")), package)
            raise error
        return SimpleNamespace(run=run)
    service = AskService(Settings(_env_file=None), workflow_factory=build)
    with pytest.raises(type(error)):
        with capture_diagnostics() as diagnostic:
            service.ask(AskRequest("q"))
    assert diagnostic.to_dict()["retrieval"] is not None
    assert diagnostic.timeline["request_finished"] is not None
    assert diagnostic.outcome["generation_status"] == ("cancelled" if isinstance(error, RequestCancelled) else "failed")
    check_cancelled()


@pytest.mark.parametrize("state", ["READY", "PARTIAL", "EMPTY", "RETRIEVAL_FAILED"])
def test_public_answer_builder_supports_offline_replay(tmp_path, state):
    from devcontext.request import AnswerOptions, UserRequest
    package = replace(make_package(), retrieval_state=state)
    class AnswerOnly:
        def run(self, request, evidence, on_section=None):
            assert evidence.evidence_catalog.labels() == package.evidence_catalog.labels()
            assert evidence.context_bundle.to_dict() == package.context_bundle.to_dict()
            return evidence.retrieval_state
    with capture_diagnostics() as diagnostic:
        record_package(UserRequest(package.original_query, AnswerOptions("teach")), package)
        diagnostic.save(tmp_path / "snapshot.json")
    factory = WorkflowFactory(llm_client_factory=forbidden, retrieval_service_factory=forbidden,
        graph_store_factory=forbidden, teaching_workflow_factory=lambda settings: AnswerOnly())
    assert replay_answer(tmp_path / "snapshot.json", factory.build_teaching_workflow(Settings(_env_file=None))) == state


def test_production_application_has_no_cli_or_evaluation_dependency():
    root = Path(__file__).resolve().parents[1] / "src/devcontext/application"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("devcontext.cli", "devcontext.evaluation"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "globals"


def test_coverage_override_keeps_default_fast_planner():
    from devcontext.agentic.fast import FastRetrievalPlanner, FastActionPlanner
    settings = Settings(_env_file=None, answer_engine_enabled=True, answer_profile="fast",
                        tool_agent_enabled=False, symbol_graph_enabled=False)
    checker = SimpleNamespace(check=forbidden)
    factory = WorkflowFactory(llm_client_factory=forbidden,
        retrieval_service_factory=lambda config: SimpleNamespace(),
        graph_store_factory=lambda *args: SimpleNamespace())
    engine = factory.build_retrieval_engine(settings, overrides=RetrievalOverrides(coverage_checker=checker))
    assert isinstance(engine.evidence_planner, FastRetrievalPlanner)
    assert isinstance(engine.action_planner, FastActionPlanner)
