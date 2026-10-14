from __future__ import annotations
from devcontext.llm.factory import create_llm_client
from collections.abc import Callable
from dataclasses import dataclass
from devcontext.code_graph.store import CodeGraphStore
from devcontext.agentic import AgenticRetrievalWorkflow, ContextSufficiencyChecker, CoverageChecker, EvidenceDrivenWorkflow, RetrievalController, RetrievalEngine, RetrievalObserver, SearchActionPlanner, TargetedQueryRewriter
from devcontext.answer import ANSWER_MAX_TOKENS, EXPLAIN_ANSWER_MAX_TOKENS, AnswerGenerator, AnswerPlanner, AnswerReviewer
from devcontext.config import Settings
from devcontext.context import ContextBuilder
from devcontext.evidence import SourcePolicy, default_source_policy_path
from devcontext.llm import DeepSeekLLMClient, LLMClient
from devcontext.explanation import EXPLANATION_PLANNER_MAX_TOKENS, ExplanationPlanner, SectionComposer, TeachingExplanationWorkflow, TeachingReviewer
from devcontext.explanation.composer import COMPOSER_MAX_TOKENS
from devcontext.explanation.reviewer import REVIEWER_MAX_TOKENS as TEACHING_REVIEWER_MAX_TOKENS
from devcontext.explanation.writer import TEACHING_ANSWER_MAX_TOKENS, TeachingWriter
from devcontext.explanation.micro import TeachingRuntimeOptions, MicroExplanationPlanner
from devcontext.explanation.stream_writer import SingleStreamingTeachingWriter
from devcontext.planning import EvidencePlan, EvidencePlanner, QuestionPlanner
from devcontext.retrieval import RetrievalPolicy, RetrievalService
from devcontext.routing import QueryRouter

LEGACY_TOP_K = 5
LEGACY_MAX_CHARS = 6000
PLANNED_TOP_K = 12
PLANNED_MAX_CHARS = 10000

# Evidence planning emits several structured requirement objects, so it needs more
# room than the other short calls: reasoning tokens can exhaust a smaller budget
# before the JSON is emitted, which surfaces as finish_reason=length.
PLANNER_MAX_TOKENS = 8192

# Coverage checking emits one status object per requirement, and its prompt
# carries each requirement's evidence bodies. At 4096 the reasoning alone
# exhausted the cap before the JSON was emitted, so every round came back
# finish_reason=length and all requirements were reported UNVERIFIED - which
# reaches the user as "insufficient evidence" on a question the pipeline had
# already retrieved the evidence for. Measured 2026-09-30: 6 requirements over
# the full token-bucket evidence set needs far more than 4096.
SUB_QUESTION_SUFFICIENCY_MAX_TOKENS = 32768

# The rewriter's prompt carries every missing aspect, and per-requirement judging
# produces a much longer gap list than the old source-type check did.
REWRITE_MAX_TOKENS = 4096
ANSWER_PLANNER_MAX_TOKENS = 8192
# The judge reads two full detailed answers, so it needs the same headroom as the
# draft and reviewer; at 8192 its reasoning alone exhausted the cap and the verdict
# came back as finish_reason=length.
JUDGE_MAX_TOKENS = 32768
REVIEWER_MAX_TOKENS = 32768


@dataclass(frozen=True, slots=True)
class RetrievalOverrides:
    """Explicit retrieval components supplied by experiments, outside production assembly."""

    evidence_planner: object | None = None
    action_planner: object | None = None
    coverage_checker: object | None = None


class WorkflowFactory:
    """Compose existing workflows with explicit external dependencies."""

    def __init__(self, *, observer: RetrievalObserver | None = None,
                 llm_client_factory: Callable[..., LLMClient] | None = None,
                 deepseek_client_type: type[DeepSeekLLMClient] | None = None,
                 retrieval_service_factory: Callable[[Settings], RetrievalService] | None = None,
                 graph_store_factory: Callable[..., CodeGraphStore] | None = None,
                 retrieval_engine_factory: Callable[..., RetrievalEngine] | None = None,
                 teaching_workflow_factory: Callable[[Settings], TeachingExplanationWorkflow] | None = None):
        self.observer = observer
        self.client_factory = llm_client_factory or create_llm_client
        self.deepseek_client_type = deepseek_client_type or DeepSeekLLMClient
        self.retrieval_service_factory = retrieval_service_factory or RetrievalService
        self.graph_store_factory = graph_store_factory or CodeGraphStore
        self.retrieval_engine_factory = retrieval_engine_factory
        self.teaching_workflow_factory = teaching_workflow_factory

    def build_answer_workflow(self, prepared):
        settings = prepared.settings.model_copy(deep=True)
        if prepared.request.no_plan:
            return self.agentic_workflow(settings, ContextBuilder(max_chars=prepared.max_chars))
        return self.planned_workflow(settings, ContextBuilder(max_chars=prepared.max_chars),
            ContextBuilder(max_chars=prepared.request.legacy_max_chars), prepared.answer_mode,
            prepared.request.max_chars, prepared.policy)

    def build_question_planner(self, settings):
        return self.question_planner(settings)

    def build_evidence_planner(self, settings, *, production=False):
        if production and settings.answer_engine_enabled and settings.answer_profile == "fast":
            from devcontext.agentic.fast import FastRetrievalPlanner
            return FastRetrievalPlanner(self.planner_llm_factory(settings))
        return self.evidence_planner(settings)

    def build_retrieval_engine(self, settings, *, observer=None, overrides=None, frozen_plan=None):
        return self.retrieval_controller(settings, observer=observer, overrides=overrides,
                                          frozen_plan=frozen_plan)

    def v3_client_factory(self, settings: Settings, factory):
        def create():
            client = factory()
            if settings.llm_provider == "openai":
                client.reasoning_effort = settings.openai_v3_reasoning_effort
            return client
        return create


    def profile_answer_factory(self, settings: Settings, *, planner=False):
        return lambda: self.client_factory(settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=(settings.deepseek_answer_planner_model if planner else settings.deepseek_answer_model) or settings.deepseek_model,
            requested_reasoning_effort=settings.answer_reasoning_effort or ("low" if settings.answer_profile == "fast" else "high"),
            max_tokens=min(settings.model_capabilities().max_output_tokens, 32768 if settings.answer_profile == "full" else (8000 if planner else 16000)),
            timeout_seconds=(settings.answer_full_planner_timeout_seconds if planner else settings.answer_full_writer_timeout_seconds) if settings.answer_profile == "full" else (settings.answer_planner_timeout_seconds if planner else settings.answer_writer_timeout_seconds),
            json_mode=planner)


    def query_router(self, settings: Settings) -> QueryRouter:
        return QueryRouter(
            lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
                legacy_model=settings.deepseek_model,
                max_tokens=1024,
            )
        )


    def short_llm_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_model,
            max_tokens=1024,
        )


    def answer_client(self, settings: Settings, *, explain: bool = False) -> DeepSeekLLMClient:
        return self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
            reasoning_effort="high" if explain else "low",
            max_tokens=EXPLAIN_ANSWER_MAX_TOKENS if explain else ANSWER_MAX_TOKENS,
        )


    def planner_llm_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_planner_model or settings.deepseek_model,
            reasoning_effort="low" if settings.llm_provider == "openai" else "high",
            max_tokens=PLANNER_MAX_TOKENS,
            timeout_seconds=120 if settings.answer_engine_enabled else (35 if settings.llm_provider == "openai" else 120),
            json_mode=True,
            **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
        )


    def rewrite_llm_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_model,
            reasoning_effort="low",
            max_tokens=REWRITE_MAX_TOKENS,
            json_mode=True,
            **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
        )


    def sub_question_sufficiency_factory(self,
        settings: Settings,
    ) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_model,
            reasoning_effort="low",
            max_tokens=SUB_QUESTION_SUFFICIENCY_MAX_TOKENS,
            json_mode=True,
            **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
        )


    def answer_planner_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_answer_planner_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=ANSWER_PLANNER_MAX_TOKENS,
            json_mode=True,
        )


    def reviewer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_reviewer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=REVIEWER_MAX_TOKENS,
            json_mode=True,
        )


    def explanation_planner_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_answer_planner_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=EXPLANATION_PLANNER_MAX_TOKENS,
            json_mode=True,
        )


    def teaching_writer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=TEACHING_ANSWER_MAX_TOKENS,
        )


    def teaching_reviewer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_reviewer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=TEACHING_REVIEWER_MAX_TOKENS,
            json_mode=True,
        )


    def composer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=COMPOSER_MAX_TOKENS,
        )


    def question_planner(self, settings: Settings) -> QuestionPlanner:
        return QuestionPlanner(self.planner_llm_factory(settings))


    def evidence_planner(self, settings: Settings) -> EvidencePlanner:
        return EvidencePlanner(self.planner_llm_factory(settings), full_teaching=settings.answer_engine_enabled and settings.answer_profile == "full")


    def answer_judge_factory(self,
        settings: Settings, model: str | None = None
    ) -> Callable[[], LLMClient]:
        """The blind judge reads two long answers and emits a tiny verdict."""
        return lambda: self.client_factory(
            settings, deepseek_client_type=self.deepseek_client_type,
            model=model,
            reasoning_effort="high",
            max_tokens=JUDGE_MAX_TOKENS,
            json_mode=True,
        )


    def agentic_workflow(self,
        settings: Settings,
        builder: ContextBuilder,
    ) -> AgenticRetrievalWorkflow:
        short_llm_factory = self.short_llm_factory(settings)
        return AgenticRetrievalWorkflow(
            router=self.query_router(settings),
            retrieval_policy=RetrievalPolicy(self.retrieval_service_factory(settings)),
            context_builder=builder,
            sufficiency_checker=ContextSufficiencyChecker(short_llm_factory),
            query_rewriter=TargetedQueryRewriter(self.rewrite_llm_factory(settings)),
            answer_generator_factory=lambda: AnswerGenerator(self.answer_client(settings)),
        )


    def planned_workflow(self,
        settings: Settings,
        planned_builder: ContextBuilder,
        legacy_builder: ContextBuilder,
        answer_mode: str = "legacy",
        context_budget_override: int | None = None,
        request_policy=None,
    ) -> EvidenceDrivenWorkflow:
        # Keep the builder arguments for one-cycle factory compatibility.  The new
        # controller derives its budget from EvidencePlan complexity unless the
        # caller explicitly supplies context_budget_override.
        _ = planned_builder, legacy_builder
        request_policy = request_policy or settings.answer_request_policy
        controller = (self.retrieval_engine_factory(settings, observer=self.observer) if self.retrieval_engine_factory is not None
                      else self.build_retrieval_engine(settings, observer=self.observer))
        teaching_workflow = self.build_teaching_workflow(settings, request_policy=request_policy) if answer_mode == "teach" else None
        return EvidenceDrivenWorkflow(
            controller,
            answer_generator_factory=lambda: AnswerGenerator(
                self.answer_client(settings, explain=answer_mode == "explain")
            ),
            answer_planner=AnswerPlanner(self.answer_planner_factory(settings)),
            answer_reviewer=AnswerReviewer(self.reviewer_factory(settings)),
            answer_mode=answer_mode,
            context_budget_override=context_budget_override,
            teaching_workflow=teaching_workflow,
            request_policy=request_policy,
        )


    def build_teaching_workflow(self, settings, *, request_policy=None):
        if self.teaching_workflow_factory is not None:
            return self.teaching_workflow_factory(settings)
        request_policy = request_policy or settings.answer_request_policy
        # The teach path keeps its own client policy: its planner and its writer
        # do different work from the explain path's, so sharing budgets and
        # effort settings between them would just constrain both.
        teaching_workflow = TeachingExplanationWorkflow(
            ExplanationPlanner(self.explanation_planner_factory(settings)),
            TeachingWriter(self.teaching_writer_factory(settings)),
            SectionComposer(self.composer_factory(settings)),
            TeachingReviewer(self.teaching_reviewer_factory(settings)),
            capabilities=settings.model_capabilities(),
            runtime_options=TeachingRuntimeOptions(settings.teaching_generation_mode),
            micro_planner=MicroExplanationPlanner(self.profile_answer_factory(settings, planner=True) if request_policy else self.explanation_planner_factory(settings), settings.model_capabilities()),
            streaming_writer=SingleStreamingTeachingWriter(self.profile_answer_factory(settings) if request_policy else self.teaching_writer_factory(settings), permissive=request_policy is not None),
        )
        if settings.teaching_generation_mode == "v3" or (request_policy and request_policy.profile == "full"):
            from devcontext.explanation.v3.planner import TeachingPlannerV3
            from devcontext.explanation.v3.writer import TeachingWriterV3
            teaching_workflow.request_timeout_seconds = settings.teaching_request_timeout_seconds
            teaching_workflow.v3_planner = TeachingPlannerV3(
                self.profile_answer_factory(settings, planner=True) if request_policy else self.v3_client_factory(settings, self.explanation_planner_factory(settings)), teaching_workflow.capabilities, teaching_workflow.estimator)
            teaching_workflow.v3_writer = TeachingWriterV3(self.profile_answer_factory(settings) if request_policy else self.v3_client_factory(settings, self.teaching_writer_factory(settings)), permissive=request_policy is not None)
        return teaching_workflow


    def retrieval_controller(self,
        settings: Settings,
        *,
        observer: RetrievalObserver | None = None,
        overrides: RetrievalOverrides | None = None,
        frozen_plan: EvidencePlan | None = None,
    ) -> RetrievalEngine:
        source_policy = SourcePolicy.from_file(
            settings.source_policy_path or default_source_policy_path()
        )
        if overrides is None or (overrides.evidence_planner is None and overrides.action_planner is None):
            evidence_planner = self.evidence_planner(settings)
            action_planner = SearchActionPlanner(self.rewrite_llm_factory(settings))
            coverage_checker = CoverageChecker(
                self.sub_question_sufficiency_factory(settings)
            )
            if settings.answer_engine_enabled and settings.answer_profile == "fast" and frozen_plan is None:
                from devcontext.agentic.fast import FastRetrievalPlanner, FastActionPlanner
                evidence_planner = FastRetrievalPlanner(self.planner_llm_factory(settings))
                action_planner = FastActionPlanner(evidence_planner)
                coverage_checker = CoverageChecker(self.sub_question_sufficiency_factory(settings), max_attempts=1)
        else:
            evidence_planner = overrides.evidence_planner or self.evidence_planner(settings)
            action_planner = overrides.action_planner or SearchActionPlanner(self.rewrite_llm_factory(settings))
            coverage_checker = overrides.coverage_checker or CoverageChecker(self.sub_question_sufficiency_factory(settings))
        if overrides is not None and overrides.coverage_checker is not None:
            coverage_checker = overrides.coverage_checker
        if frozen_plan is not None:
            from devcontext.agentic.retrieval_engine import FixedEvidencePlanner
            evidence_planner = FixedEvidencePlanner(frozen_plan)
        from devcontext.agentic.structural_coverage import combined
        from devcontext.code_graph.hydration import GraphMetadataHydrator
        hydrator = GraphMetadataHydrator(self.graph_store_factory(settings.database_url, settings.repository_name, settings.symbol_graph_query_timeout_seconds), "tool_agent" if settings.tool_agent_enabled else "auto_graph" if settings.symbol_graph_enabled else "fixed_search")
        coverage_checker = combined(coverage_checker, hydrator)
        if settings.tool_agent_enabled:
            from devcontext.agentic.tool_driven_retrieval_controller import ToolDrivenRetrievalController
            from devcontext.tool_agent.executor import ToolExecutor
            from devcontext.tool_agent.planner import AgentPlanner
            from devcontext.tool_agent.registry import ToolRegistry
            from devcontext.tool_agent.runtime import AgentRuntime
            from devcontext.tool_agent.tools import RepositoryTools
            if overrides is None or overrides.coverage_checker is None:
                coverage_checker = combined(CoverageChecker(self.sub_question_sufficiency_factory(settings), max_attempts=1), hydrator)
            tools = RepositoryTools(RetrievalPolicy(self.retrieval_service_factory(settings)),
                self.graph_store_factory(settings.database_url, settings.repository_name, settings.symbol_graph_query_timeout_seconds), observer)
            factory = lambda: self.client_factory(settings, deepseek_client_type=self.deepseek_client_type,
                legacy_model=settings.deepseek_planner_model or settings.deepseek_model,
                requested_reasoning_effort="low", max_tokens=8192, json_mode=True,
                timeout_seconds=settings.tool_agent_planner_timeout_seconds)
            return ToolDrivenRetrievalController(evidence_planner,
                AgentRuntime(AgentPlanner(factory), ToolExecutor(ToolRegistry(tools)), coverage_checker, source_policy), observer)
        return RetrievalController(
            evidence_planner=evidence_planner,
            action_planner=action_planner,
            retrieval_policy=RetrievalPolicy(self.retrieval_service_factory(settings)),
            coverage_checker=coverage_checker,
            source_policy=source_policy,
            observer=observer,
            graph_expander=self.code_graph_expander(settings),
        )


    def code_graph_expander(self, settings: Settings):
        if not settings.symbol_graph_enabled:
            return None
        from devcontext.code_graph.expansion import CodeGraphExpander
        return CodeGraphExpander(self.graph_store_factory(settings.database_url, settings.repository_name,
                                               settings.symbol_graph_query_timeout_seconds))


