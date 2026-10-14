from __future__ import annotations
from devcontext.llm.factory import create_llm_client
from collections.abc import Callable
from devcontext.agentic import AgenticRetrievalWorkflow, ContextSufficiencyChecker, CoverageChecker, EvidenceDrivenWorkflow, RetrievalController, RetrievalEngine, RetrievalObserver, SearchActionPlanner, TargetedQueryRewriter
from devcontext.answer import ANSWER_MAX_TOKENS, EXPLAIN_ANSWER_MAX_TOKENS, AnswerGenerator, AnswerPlanner, AnswerReviewer
from devcontext.config import Settings
from devcontext.context import ContextBuilder
from devcontext.evidence import SourcePolicy, default_source_policy_path
from devcontext.evaluation.retrieval_workflow_runner import FrozenEvidencePlanner, FrozenSearchActionPlanner, OracleCoverageChecker
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


class WorkflowFactory:
    """Request-local workflow assembly; optional overrides preserve CLI adapters."""

    def __init__(self, dependencies=None, observer=None):
        self.dependencies = dependencies or {}
        self.observer = observer

    def _resolve(self, name):
        if name in self.dependencies:
            return self.dependencies[name]
        return getattr(self, name) if hasattr(self, name) else globals()[name]

    def _v3_client_factory(self, settings: Settings, factory):
        def create():
            client = factory()
            if settings.llm_provider == "openai":
                client.reasoning_effort = settings.openai_v3_reasoning_effort
            return client
        return create


    def _profile_answer_factory(self, settings: Settings, *, planner=False):
        return lambda: self._resolve("create_llm_client")(settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=(settings.deepseek_answer_planner_model if planner else settings.deepseek_answer_model) or settings.deepseek_model,
            requested_reasoning_effort=settings.answer_reasoning_effort or ("low" if settings.answer_profile == "fast" else "high"),
            max_tokens=min(settings.model_capabilities().max_output_tokens, 32768 if settings.answer_profile == "full" else (8000 if planner else 16000)),
            timeout_seconds=(settings.answer_full_planner_timeout_seconds if planner else settings.answer_full_writer_timeout_seconds) if settings.answer_profile == "full" else (settings.answer_planner_timeout_seconds if planner else settings.answer_writer_timeout_seconds),
            json_mode=planner)


    def _query_router(self, settings: Settings) -> QueryRouter:
        return self._resolve("QueryRouter")(
            lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
                legacy_model=settings.deepseek_model,
                max_tokens=1024,
            )
        )


    def _short_llm_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_model,
            max_tokens=1024,
        )


    def _answer_client(self, settings: Settings, *, explain: bool = False) -> DeepSeekLLMClient:
        return self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
            reasoning_effort="high" if explain else "low",
            max_tokens=EXPLAIN_ANSWER_MAX_TOKENS if explain else ANSWER_MAX_TOKENS,
        )


    def _planner_llm_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_planner_model or settings.deepseek_model,
            reasoning_effort="low" if settings.llm_provider == "openai" else "high",
            max_tokens=PLANNER_MAX_TOKENS,
            timeout_seconds=120 if settings.answer_engine_enabled else (35 if settings.llm_provider == "openai" else 120),
            json_mode=True,
            **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
        )


    def _rewrite_llm_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_model,
            reasoning_effort="low",
            max_tokens=REWRITE_MAX_TOKENS,
            json_mode=True,
            **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
        )


    def _sub_question_sufficiency_factory(self,
        settings: Settings,
    ) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_model,
            reasoning_effort="low",
            max_tokens=SUB_QUESTION_SUFFICIENCY_MAX_TOKENS,
            json_mode=True,
            **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
        )


    def _answer_planner_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_answer_planner_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=ANSWER_PLANNER_MAX_TOKENS,
            json_mode=True,
        )


    def _reviewer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_reviewer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=REVIEWER_MAX_TOKENS,
            json_mode=True,
        )


    def _explanation_planner_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_answer_planner_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=EXPLANATION_PLANNER_MAX_TOKENS,
            json_mode=True,
        )


    def _teaching_writer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=TEACHING_ANSWER_MAX_TOKENS,
        )


    def _teaching_reviewer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_reviewer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=TEACHING_REVIEWER_MAX_TOKENS,
            json_mode=True,
        )


    def _composer_factory(self, settings: Settings) -> Callable[[], LLMClient]:
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
            reasoning_effort="high",
            max_tokens=COMPOSER_MAX_TOKENS,
        )


    def _question_planner(self, settings: Settings) -> QuestionPlanner:
        return self._resolve("QuestionPlanner")(self._resolve("_planner_llm_factory")(settings))


    def _evidence_planner(self, settings: Settings) -> EvidencePlanner:
        return self._resolve("EvidencePlanner")(self._resolve("_planner_llm_factory")(settings), full_teaching=settings.answer_engine_enabled and settings.answer_profile == "full")


    def _answer_judge_factory(self,
        settings: Settings, model: str | None = None
    ) -> Callable[[], LLMClient]:
        """The blind judge reads two long answers and emits a tiny verdict."""
        return lambda: self._resolve("create_llm_client")(
            settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
            model=model,
            reasoning_effort="high",
            max_tokens=JUDGE_MAX_TOKENS,
            json_mode=True,
        )


    def _agentic_workflow(self,
        settings: Settings,
        builder: ContextBuilder,
    ) -> AgenticRetrievalWorkflow:
        short_llm_factory = self._resolve("_short_llm_factory")(settings)
        return self._resolve("AgenticRetrievalWorkflow")(
            router=self._resolve("_query_router")(settings),
            retrieval_policy=self._resolve("RetrievalPolicy")(self._resolve("RetrievalService")(settings)),
            context_builder=builder,
            sufficiency_checker=self._resolve("ContextSufficiencyChecker")(short_llm_factory),
            query_rewriter=self._resolve("TargetedQueryRewriter")(self._resolve("_rewrite_llm_factory")(settings)),
            answer_generator_factory=lambda: self._resolve("AnswerGenerator")(self._resolve("_answer_client")(settings)),
        )


    def _planned_workflow(self,
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
        controller = self._resolve("_retrieval_controller")(settings, observer=self.observer) if self.observer is not None else self._resolve("_retrieval_controller")(settings)
        teaching_workflow = None
        if answer_mode == "teach":
            # The teach path keeps its own client policy: its planner and its writer
            # do different work from the explain path's, so sharing budgets and
            # effort settings between them would just constrain both.
            teaching_workflow = self._resolve("TeachingExplanationWorkflow")(
                self._resolve("ExplanationPlanner")(self._resolve("_explanation_planner_factory")(settings)),
                self._resolve("TeachingWriter")(self._resolve("_teaching_writer_factory")(settings)),
                self._resolve("SectionComposer")(self._resolve("_composer_factory")(settings)),
                self._resolve("TeachingReviewer")(self._resolve("_teaching_reviewer_factory")(settings)),
                capabilities=settings.model_capabilities(),
                runtime_options=self._resolve("TeachingRuntimeOptions")(settings.teaching_generation_mode),
                micro_planner=self._resolve("MicroExplanationPlanner")(self._resolve("_profile_answer_factory")(settings, planner=True) if request_policy else self._resolve("_explanation_planner_factory")(settings), settings.model_capabilities()),
                streaming_writer=self._resolve("SingleStreamingTeachingWriter")(self._resolve("_profile_answer_factory")(settings) if request_policy else self._resolve("_teaching_writer_factory")(settings), permissive=request_policy is not None),
            )
            if settings.teaching_generation_mode == "v3" or (request_policy and request_policy.profile == "full"):
                from devcontext.explanation.v3.planner import TeachingPlannerV3
                from devcontext.explanation.v3.writer import TeachingWriterV3
                teaching_workflow.request_timeout_seconds = settings.teaching_request_timeout_seconds
                teaching_workflow.v3_planner = TeachingPlannerV3(
                    self._resolve("_profile_answer_factory")(settings, planner=True) if request_policy else self._resolve("_v3_client_factory")(settings, self._resolve("_explanation_planner_factory")(settings)), teaching_workflow.capabilities, teaching_workflow.estimator)
                teaching_workflow.v3_writer = TeachingWriterV3(self._resolve("_profile_answer_factory")(settings) if request_policy else self._resolve("_v3_client_factory")(settings, self._resolve("_teaching_writer_factory")(settings)), permissive=request_policy is not None)
        return self._resolve("EvidenceDrivenWorkflow")(
            controller,
            answer_generator_factory=lambda: self._resolve("AnswerGenerator")(
                self._resolve("_answer_client")(settings, explain=answer_mode == "explain")
            ),
            answer_planner=self._resolve("AnswerPlanner")(self._resolve("_answer_planner_factory")(settings)),
            answer_reviewer=self._resolve("AnswerReviewer")(self._resolve("_reviewer_factory")(settings)),
            answer_mode=answer_mode,
            context_budget_override=context_budget_override,
            teaching_workflow=teaching_workflow,
            request_policy=request_policy,
        )


    def _retrieval_controller(self,
        settings: Settings,
        *,
        observer: RetrievalObserver | None = None,
        frozen_case: dict | None = None,
        frozen_plan: EvidencePlan | None = None,
        shared_coverage_factory=None,
    ) -> RetrievalEngine:
        source_policy = SourcePolicy.from_file(
            settings.source_policy_path or self._resolve("default_source_policy_path")()
        )
        if frozen_case is None:
            evidence_planner = self._resolve("_evidence_planner")(settings)
            action_planner = self._resolve("SearchActionPlanner")(self._resolve("_rewrite_llm_factory")(settings))
            coverage_checker = self._resolve("CoverageChecker")(
                self._resolve("_sub_question_sufficiency_factory")(settings)
            )
            if settings.answer_engine_enabled and settings.answer_profile == "fast" and frozen_plan is None:
                from devcontext.agentic.fast import FastRetrievalPlanner, FastActionPlanner
                evidence_planner = FastRetrievalPlanner(self._resolve("_planner_llm_factory")(settings))
                action_planner = FastActionPlanner(evidence_planner)
                coverage_checker = self._resolve("CoverageChecker")(self._resolve("_sub_question_sufficiency_factory")(settings), max_attempts=1)
        else:
            evidence_planner = self._resolve("FrozenEvidencePlanner")(frozen_case)
            action_planner = self._resolve("FrozenSearchActionPlanner")(frozen_case)
            coverage_checker = self._resolve("OracleCoverageChecker")(frozen_case)
        if frozen_plan is not None:
            from devcontext.agentic.retrieval_engine import FixedEvidencePlanner
            evidence_planner = FixedEvidencePlanner(frozen_plan)
        if shared_coverage_factory is not None:
            coverage_checker = shared_coverage_factory()
        from devcontext.agentic.structural_coverage import combined
        from devcontext.code_graph.hydration import GraphMetadataHydrator
        from devcontext.code_graph.store import CodeGraphStore
        hydrator = GraphMetadataHydrator(CodeGraphStore(settings.database_url, settings.repository_name, settings.symbol_graph_query_timeout_seconds), "tool_agent" if settings.tool_agent_enabled else "auto_graph" if settings.symbol_graph_enabled else "fixed_search")
        coverage_checker = combined(coverage_checker, hydrator)
        if settings.tool_agent_enabled:
            from devcontext.agentic.tool_driven_retrieval_controller import ToolDrivenRetrievalController
            from devcontext.code_graph.store import CodeGraphStore
            from devcontext.tool_agent.executor import ToolExecutor
            from devcontext.tool_agent.planner import AgentPlanner
            from devcontext.tool_agent.registry import ToolRegistry
            from devcontext.tool_agent.runtime import AgentRuntime
            from devcontext.tool_agent.tools import RepositoryTools
            if shared_coverage_factory is None and frozen_case is None:
                coverage_checker = combined(self._resolve("CoverageChecker")(self._resolve("_sub_question_sufficiency_factory")(settings), max_attempts=1), hydrator)
            tools = RepositoryTools(self._resolve("RetrievalPolicy")(self._resolve("RetrievalService")(settings)),
                CodeGraphStore(settings.database_url, settings.repository_name, settings.symbol_graph_query_timeout_seconds), observer)
            factory = lambda: self._resolve("create_llm_client")(settings, deepseek_client_type=self._resolve("DeepSeekLLMClient"),
                legacy_model=settings.deepseek_planner_model or settings.deepseek_model,
                requested_reasoning_effort="low", max_tokens=8192, json_mode=True,
                timeout_seconds=settings.tool_agent_planner_timeout_seconds)
            return ToolDrivenRetrievalController(evidence_planner,
                AgentRuntime(AgentPlanner(factory), ToolExecutor(ToolRegistry(tools)), coverage_checker, source_policy), observer)
        return self._resolve("RetrievalController")(
            evidence_planner=evidence_planner,
            action_planner=action_planner,
            retrieval_policy=self._resolve("RetrievalPolicy")(self._resolve("RetrievalService")(settings)),
            coverage_checker=coverage_checker,
            source_policy=source_policy,
            observer=observer,
            graph_expander=self._resolve("_code_graph_expander")(settings),
        )


    def _code_graph_expander(self, settings: Settings):
        if not settings.symbol_graph_enabled:
            return None
        from devcontext.code_graph.expansion import CodeGraphExpander
        from devcontext.code_graph.store import CodeGraphStore
        return CodeGraphExpander(CodeGraphStore(settings.database_url, settings.repository_name,
                                               settings.symbol_graph_query_timeout_seconds))


