"""Transport-independent application operations and request-local execution."""
from dataclasses import dataclass, replace
import math
from collections.abc import Callable

from devcontext.answer_policy import RequestPolicy, resolve_policy
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


@dataclass(frozen=True, slots=True)
class ResolvedAsk:
    request: AskRequest
    settings: Settings
    policy: RequestPolicy | None
    top_k: int
    max_chars: int
    answer_mode: str


@dataclass(frozen=True, slots=True)
class PlanRequest:
    query: str
    format: str = "evidence"
    answer_mode: str = "teach"
    profile: str | None = None
    reasoning_effort: str | None = None
    hard_timeout: float | None = None
    teaching_generation_mode: str | None = None


def _validate(request: AskRequest):
    if not isinstance(request.query, str) or not request.query.strip():
        raise ValueError("query must be non-empty")
    if request.answer_mode not in {"teach", "explain", "legacy"}:
        raise ValueError("invalid answer mode")
    if request.profile not in {None, "fast", "full"}:
        raise ValueError("invalid profile")
    if request.reasoning_effort not in {None, "low", "medium", "high"}:
        raise ValueError("invalid reasoning effort")
    if request.teaching_generation_mode not in {None, "single_stream", "v3", "multi_pass"}:
        raise ValueError("invalid teaching generation mode")
    for name in ("top_k", "max_chars", "legacy_max_chars"):
        value = getattr(request, name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError(f"{name} must be a positive integer")
    if request.hard_timeout is not None and (isinstance(request.hard_timeout, bool)
            or not isinstance(request.hard_timeout, (int, float))
            or not math.isfinite(request.hard_timeout) or request.hard_timeout <= 0):
        raise ValueError("hard timeout must be positive and finite")


def _configure_request(settings: Settings, request: AskRequest):
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
        settings = settings.model_copy(update={"teaching_generation_mode": engine, "answer_synthesis_mode": "planned"})
    return settings, policy


def resolve_request(settings: Settings, request: AskRequest) -> ResolvedAsk:
    """Normalize once, without constructing model clients or retrieval services."""
    _validate(request)
    if request.plan_only:
        raise ValueError("plan_only requires AskService.plan(PlanRequest(...))")
    configured, policy = _configure_request(settings, request)
    if policy is not None and policy.hard_timeout_seconds is not None and not math.isfinite(policy.hard_timeout_seconds):
        raise ValueError("hard timeout must be positive and finite")
    return ResolvedAsk(request, configured, policy,
        request.top_k if request.top_k is not None else (LEGACY_TOP_K if request.no_plan else PLANNED_TOP_K),
        request.max_chars if request.max_chars is not None else (LEGACY_MAX_CHARS if request.no_plan else PLANNED_MAX_CHARS),
        "legacy" if request.no_plan else request.answer_mode)


class AskService:
    def __init__(self, settings: Settings | None = None, *, factory_builder: Callable | None = None,
                 workflow_factory=None, legacy_factory=None):
        self.settings = (settings if settings is not None else Settings()).model_copy(deep=True)
        self.factory_builder = factory_builder
        # Explicit high-level test/embedding seams; never CLI globals.
        self.workflow_factory = workflow_factory
        self.legacy_factory = legacy_factory

    def prepare(self, request: AskRequest) -> ResolvedAsk:
        return resolve_request(self.settings, request)

    def execute(self, prepared: ResolvedAsk, *, observer=None, on_section=None, on_evidence=None,
                cancellation=None, on_event=None):
        scoped = replace(prepared, settings=prepared.settings.model_copy(deep=True))
        with execution_scope(cancellation, on_evidence, on_event):
            factory = (self.factory_builder or WorkflowFactory)(observer=observer)
            if scoped.request.no_plan and self.legacy_factory is not None:
                workflow = self.legacy_factory(scoped.settings, ContextBuilder(max_chars=scoped.max_chars))
            elif not scoped.request.no_plan and self.workflow_factory is not None:
                workflow = self.workflow_factory(scoped.settings, ContextBuilder(max_chars=scoped.max_chars),
                    ContextBuilder(max_chars=scoped.request.legacy_max_chars), scoped.answer_mode,
                    scoped.request.max_chars)
            else:
                workflow = factory.build_answer_workflow(scoped)
            check_cancelled()
            result = workflow.run(scoped.request.query, scoped.top_k,
                **({"on_section": on_section} if on_section is not None else {}))
            check_cancelled()
            return result

    def ask(self, request: AskRequest, *, observer=None, on_section=None, on_evidence=None,
            cancellation=None, on_event=None):
        return self.execute(self.prepare(request), observer=observer, on_section=on_section,
                            on_evidence=on_evidence, cancellation=cancellation, on_event=on_event)

    def plan(self, request: PlanRequest, *, observer=None, cancellation=None):
        if request.format not in {"legacy", "evidence"}:
            raise ValueError("invalid plan format")
        options = AskRequest(request.query, answer_mode=request.answer_mode, profile=request.profile,
            reasoning_effort=request.reasoning_effort, hard_timeout=request.hard_timeout,
            teaching_generation_mode=request.teaching_generation_mode, plan_only=True)
        _validate(options)
        settings, _ = _configure_request(self.settings, options)
        with execution_scope(cancellation):
            factory = (self.factory_builder or WorkflowFactory)(observer=observer)
            planner = (factory.build_question_planner(settings) if request.format == "legacy"
                       else factory.build_evidence_planner(settings))
            check_cancelled()
            result = planner.plan(request.query)
            check_cancelled()
            return result
