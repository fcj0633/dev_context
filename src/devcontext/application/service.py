"""Public application entry point shared by CLI and Web."""
from dataclasses import dataclass

from devcontext.answer_policy import resolve_policy
from devcontext.config import Settings
from devcontext.context import ContextBuilder
from devcontext.execution import check_cancelled, execution_scope
from .assembly import WorkflowFactory, LEGACY_TOP_K, LEGACY_MAX_CHARS, PLANNED_TOP_K, PLANNED_MAX_CHARS


@dataclass(frozen=True, slots=True)
class AskRequest:
    query: str
    answer_mode: str = "teach"
    profile: str | None = None
    reasoning_effort: str | None = None
    hard_timeout: float | None = None
    teaching_generation_mode: str | None = None
    top_k: int | None = None
    max_chars: int | None = None
    legacy_max_chars: int = LEGACY_MAX_CHARS
    no_plan: bool = False
    plan_only: bool = False

    @classmethod
    def from_cli(cls, args):
        return cls(**{name: getattr(args, name) for name in cls.__dataclass_fields__ if hasattr(args, name)})


def resolve_request(settings: Settings, request: AskRequest):
    settings = settings.model_copy(deep=True)
    policy = None
    mapping = {"single_stream": "fast", "v3": "full"}
    engine = request.teaching_generation_mode
    controls = request.profile is not None or request.reasoning_effort is not None
    if controls and (request.answer_mode != "teach" or engine == "multi_pass" or request.no_plan or request.plan_only):
        raise ValueError("--profile/--reasoning-effort conflict with legacy/explain/multi_pass or plan-only/no-plan")
    if engine in mapping and request.profile is not None and mapping[engine] != request.profile:
        raise ValueError("--profile conflicts with --teaching-generation-mode")
    if request.answer_mode == "teach" and engine != "multi_pass" and not request.no_plan and not request.plan_only:
        policy = resolve_policy(request.query, settings, profile=request.profile or mapping.get(engine),
                                reasoning_effort=request.reasoning_effort, hard_timeout=request.hard_timeout)
        settings = settings.model_copy(update={"answer_engine_enabled": True, "answer_profile": policy.profile,
            "answer_reasoning_effort": policy.reasoning_effort, "answer_request_policy": policy,
            "teaching_generation_mode": "single_stream" if policy.profile == "fast" else "v3"})
    elif request.hard_timeout is not None:
        raise ValueError("--hard-timeout requires the Fast/Full answering workflow")
    if engine is not None:
        if request.answer_mode != "teach" or request.no_plan or request.plan_only:
            raise ValueError("--teaching-generation-mode requires the teach answering workflow")
        settings = settings.model_copy(update={"teaching_generation_mode": engine})
    return settings, policy


class AskService:
    def __init__(self, settings=None, *, workflow_factory=None, legacy_factory=None):
        self.settings = settings or Settings()
        self.workflow_factory = workflow_factory
        self.legacy_factory = legacy_factory

    def ask(self, request: AskRequest, *, observer=None, on_section=None, on_evidence=None,
            cancellation=None, configure=True):
        settings = resolve_request(self.settings, request)[0] if configure else self.settings.model_copy(deep=True)
        factory = WorkflowFactory(observer=observer)
        top_k = request.top_k if request.top_k is not None else (LEGACY_TOP_K if request.no_plan else PLANNED_TOP_K)
        max_chars = request.max_chars if request.max_chars is not None else (LEGACY_MAX_CHARS if request.no_plan else PLANNED_MAX_CHARS)
        with execution_scope(cancellation, on_evidence):
            if request.no_plan:
                workflow = (self.legacy_factory or factory._agentic_workflow)(settings, ContextBuilder(max_chars=max_chars))
            else:
                workflow = (self.workflow_factory or factory._planned_workflow)(settings, ContextBuilder(max_chars=max_chars),
                    ContextBuilder(max_chars=request.legacy_max_chars), request.answer_mode, request.max_chars)
            check_cancelled()
            result = workflow.run(request.query, top_k, **({"on_section": on_section} if on_section is not None else {}))
            check_cancelled()
            return result
