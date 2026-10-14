from types import SimpleNamespace
import pytest

from devcontext.application import AskRequest, AskService, resolve_request
from devcontext.application.assembly import WorkflowFactory
from devcontext.config import Settings
from devcontext.execution import CancellationToken, RequestCancelled, check_cancelled, publish_evidence


@pytest.mark.parametrize("profile,mode,effort", [("fast", "single_stream", "low"), ("full", "v3", "high")])
def test_request_configuration_isolated_and_assembly_receives_hooks(profile, mode, effort):
    settings = Settings(_env_file=None, answer_profile="full")
    copies, workflows, received = [], [], []
    def factory(config, *args):
        copies.append(config)
        class Workflow:
            def run(self, question, top_k, on_section=None):
                publish_evidence("actual-bundle")
                on_section("validated")
                return "result"
        value = Workflow()
        workflows.append(value)
        return value
    service = AskService(settings, workflow_factory=factory)
    for _ in range(2):
        assert service.ask(AskRequest("q", profile=profile), on_section=received.append, on_evidence=received.append) == "result"
    assert copies[0] is not copies[1] and workflows[0] is not workflows[1]
    assert copies[0].answer_profile == profile and copies[0].teaching_generation_mode == mode
    assert copies[0].answer_request_policy.reasoning_effort == effort
    assert settings.answer_request_policy is None and not settings.answer_engine_enabled
    assert received == ["actual-bundle", "validated"] * 2


def test_real_workflow_factory_injects_observer_without_cli_import(monkeypatch):
    settings = Settings(_env_file=None, deepseek_api_key="test-key")
    observer = object()
    controller_calls = []
    def controller(settings, **kwargs):
        controller_calls.append(kwargs)
        return object()
    # Construct the actual teaching chain with its ordinary factories, but no calls to a model or DB.
    factory = WorkflowFactory(retrieval_engine_factory=controller, observer=observer)
    prepared = resolve_request(settings, AskRequest("q", profile="fast"))
    workflow = factory.build_answer_workflow(prepared)
    assert controller_calls == [{"observer": observer}]
    assert workflow.teaching_workflow is not None and workflow.request_policy.profile == "fast"


def test_cancel_scope_cannot_leak_to_next_request():
    token = CancellationToken()
    token.cancel()
    called = []
    service = AskService(Settings(_env_file=None), workflow_factory=lambda *args: called.append(args))
    with pytest.raises(RequestCancelled):
        service.ask(AskRequest("q"), cancellation=token)
    assert not called
    check_cancelled()
