from __future__ import annotations

from devcontext.llm.factory import create_llm_client

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from devcontext.agentic import (
    AgenticAnswerResult,
    AgenticRetrievalWorkflow,
    ContextSufficiencyChecker,
    CoverageChecker,
    EvidenceDrivenWorkflow,
    PlannedRetrievalWorkflow,
    RetrievalController,
    RetrievalEngine,
    RetrievalObserver,
    SearchActionPlanner,
    SufficiencyResult,
    TargetedQueryRewriter,
)
from devcontext.answer import (
    ANSWER_MAX_TOKENS,
    EXPLAIN_ANSWER_MAX_TOKENS,
    AnswerGenerator,
    AnswerPlanner,
    AnswerReviewer,
    format_source,
)
from devcontext.config import Settings, project_root
from devcontext.context import ContextBuilder
from devcontext.embedding.client import BailianEmbeddingClient
from devcontext.evidence import SourcePolicy, default_source_policy_path
from devcontext.evaluation.answer_quality_runner import (
    PairwiseEvaluationConfig,
    run_answer_quality_evaluation,
)
from devcontext.evaluation.runner import evaluate
from devcontext.evaluation.retrieval_workflow_runner import (
    FrozenEvidencePlanner,
    FrozenSearchActionPlanner,
    OracleCoverageChecker,
    RetrievalTraceRecorder,
    build_workflow_baseline,
    compare_workflow_baseline,
    run_retrieval_workflow_evaluation,
)
from devcontext.ingestion.pipeline import ingest
from devcontext.llm import DeepSeekLLMClient, LLMClient
from devcontext.models import SearchResult
from devcontext.observability import PerfRecorder, capture
from devcontext.observability.report import build_perf_report, render_summary
from devcontext.explanation import (
    EXPLANATION_PLANNER_MAX_TOKENS,
    ExplanationPlanner,
    SectionComposer,
    TeachingExplanationWorkflow,
    TeachingReviewer,
)
from devcontext.explanation.composer import COMPOSER_MAX_TOKENS
from devcontext.explanation.reviewer import REVIEWER_MAX_TOKENS as TEACHING_REVIEWER_MAX_TOKENS
from devcontext.explanation.writer import TEACHING_ANSWER_MAX_TOKENS, TeachingWriter
from devcontext.explanation.micro import TeachingRuntimeOptions, MicroExplanationPlanner
from devcontext.explanation.stream_writer import SingleStreamingTeachingWriter
from devcontext.planning import EvidencePlan, EvidencePlanner, QuestionPlan, QuestionPlanner
from devcontext.request import ANSWER_MODES
from devcontext.retrieval import RetrievalPolicy, RetrievalService
from devcontext.routing import QueryRouter, RouteDecision
from devcontext.storage import ChunkStore

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devcontext",
        description="Index and retrieve context from my12306 Java source and Markdown docs.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create extensions, table, and indexes")
    subparsers.add_parser("smoke-api", help="Verify Bailian embedding connectivity")
    subparsers.add_parser("ingest", help="Fully rebuild the my12306 index")
    graph = subparsers.add_parser("graph", help="Query static repository-internal code relations")
    graph.add_argument("operation", choices=("symbol", "callers", "callees", "implementations", "hierarchy"))
    graph.add_argument("value", help="Name for symbol lookup, complete symbol_key for relations")
    graph.add_argument("--repository")
    graph.add_argument("--json", action="store_true", help="Print structured JSON")
    graph_evaluation = subparsers.add_parser("evaluate-symbol-graph", help="Compare Graph OFF/ON with fixed candidates and oracle coverage")
    graph_evaluation.add_argument("--fixture", action="store_true", help="Analyze and index an isolated source fixture, then remove its snapshot")
    graph_evaluation.add_argument("--cases", type=Path)
    graph_evaluation.add_argument("--output", type=Path)
    tool_evaluation = subparsers.add_parser("evaluate-tool-agent", help="Paired Fixed / Auto Graph / Tool Agent retrieval evaluation")
    tool_evaluation.add_argument("--cases", type=Path, action="append", help="Workflow-format cases, optionally with allowed_tool_sequences")
    tool_evaluation.add_argument("--checker", choices=("semantic", "oracle"), default="semantic")
    tool_evaluation.add_argument("--runs", type=int, default=3)
    tool_evaluation.add_argument("--top-k", type=int, default=12)
    tool_evaluation.add_argument("--limit", type=int)
    tool_evaluation.add_argument("--output", type=Path)

    search = subparsers.add_parser("search", help="Search indexed chunks")
    search.add_argument("--strategy", choices=("keyword", "vector", "hybrid"), default="hybrid")
    search.add_argument("--query", required=True)
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--format", choices=("table", "json"), default="table")

    context = subparsers.add_parser("context", help="Build LLM-ready context from hybrid search")
    context.add_argument("query")
    context.add_argument("--top-k", type=int, default=5)
    context.add_argument("--max-chars", type=int, default=6000)

    ask = subparsers.add_parser("ask", help="Answer a question from grounded project context")
    ask.add_argument("query")
    ask.add_argument("--top-k", type=int, default=None)
    ask.add_argument("--max-chars", type=int, default=None)
    ask.add_argument(
        "--no-plan",
        action="store_true",
        help="Deprecated: use the legacy single-query workflow",
    )
    ask.add_argument(
        "--plan-only",
        action="store_true",
        help="Print a plan as JSON without retrieving or answering",
    )
    ask.add_argument(
        "--plan-format",
        choices=("evidence", "legacy"),
        default="evidence",
        help="Choose the plan schema printed by --plan-only",
    )
    ask.add_argument("--debug", action="store_true")
    ask.add_argument("--profile", choices=("fast", "full"), default=None)
    ask.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default=None)
    ask.add_argument("--hard-timeout", type=float, default=None)
    ask.add_argument("--teaching-generation-mode", choices=("multi_pass", "single_stream", "v3"), default=None,
                     help="Compatibility override: single_stream=fast, v3=full; multi_pass retains the legacy workflow")
    ask.add_argument(
        "--answer-mode",
        choices=ANSWER_MODES,
        default="teach",
        help=(
            "teach = planned teaching explanation (default), "
            "explain = evidence-driven explanation, "
            "legacy = template single-pass answer"
        ),
    )
    ask.add_argument(
        "--perf",
        action="store_true",
        help="Print a stage-by-stage latency summary after answering",
    )
    ask.add_argument(
        "--perf-json",
        type=Path,
        default=None,
        help="Write the machine-readable performance report to this path",
    )
    ask.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Run the question N times in this process. Repeat 1 is "
            "'first_sample'; the rest are 'repeat_sample', because every "
            "standalone ask starts its own interpreter and so has no warm state."
        ),
    )

    evaluation = subparsers.add_parser("evaluate", help="Run the retrieval benchmark")
    evaluation.add_argument("--benchmark", type=Path)
    evaluation.add_argument("--baseline", type=Path)

    retrieval_workflow = subparsers.add_parser(
        "evaluate-retrieval-workflow",
        help="Run the L1.5 RetrievalController and regression benchmark",
    )
    retrieval_workflow.add_argument(
        "--suite", choices=("l1.5", "regression", "all"), default="all"
    )
    retrieval_workflow.add_argument(
        "--mode", choices=("frozen", "live", "both"), default="frozen"
    )
    retrieval_workflow.add_argument("--cases", type=Path)
    retrieval_workflow.add_argument("--only", help="Comma-separated case ids")
    retrieval_workflow.add_argument("--limit", type=int)
    retrieval_workflow.add_argument("--runs", type=int, default=1)
    retrieval_workflow.add_argument("--baseline", type=Path)
    retrieval_workflow.add_argument("--output", type=Path)
    retrieval_workflow.add_argument("--write-baseline", type=Path)
    retrieval_workflow.add_argument("--symbol-graph-ab", action="store_true", help="Run the same suite with graph OFF and ON")

    answers = subparsers.add_parser(
        "evaluate-answers",
        help="Run the L2 answer-quality set through both answer modes and judge them blind",
    )
    answers.add_argument("--cases", type=Path)
    answers.add_argument("--only", help="Comma-separated case ids to run")
    answers.add_argument("--limit", type=int)
    answers.add_argument("--resume", action="store_true", help="Reuse the per-case journal if it exists")
    answers.add_argument("--judge-model", help="Override the model used by the blind judge")
    answers.add_argument(
        "--baseline-mode", choices=ANSWER_MODES, default="legacy",
        help="The arm to compare against",
    )
    answers.add_argument(
        "--candidate-mode", choices=ANSWER_MODES, default="explain",
        help="The arm under evaluation; the release gate runs teach against explain",
    )
    answers.add_argument("--output", type=Path, help="Where to write the JSON report")
    return parser


def _print_results(results: list, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2))
        return
    for rank, result in enumerate(results, start=1):
        identity = result.signature or result.symbol_name or result.title or "(untitled)"
        location = result.file_path
        if result.start_line:
            location += f":{result.start_line}"
        preview = " ".join(result.content.split())[:180]
        print(f"{rank:>2}. [{result.source_type}/{result.chunk_type}] score={result.score:.6f}")
        print(f"    {identity}")
        print(f"    {location}")
        print(f"    {preview}")


def _print_agentic_answer(
    query: str,
    result: AgenticAnswerResult,
    *,
    debug: bool,
    include_plan: bool = False,
    answer_already_published: bool = False,
) -> None:
    trace = result.trace
    print("Question:")
    print(query)
    if include_plan and trace.evidence_plan:
        print(f"\nEvidence Plan: {trace.evidence_plan['decision_source']}")
        for requirement in trace.evidence_plan["requirements"]:
            print(
                f"  {requirement['id']}. {requirement['target']}"
                f" — {requirement['source_requirement']}"
            )
    elif include_plan and trace.plan:
        print(f"\nPlan: {trace.plan['decision_source']}")
        print(f"Intent: {trace.plan['intent_summary']}")
        for sub_question in trace.plan["sub_questions"]:
            print(
                f"  {sub_question['id']}. {sub_question['question']}"
                f" — {sub_question['purpose']}"
            )
    print(
        f"\nRoute: {trace.route.query_type.value} "
        f"({trace.route.decision_source.value})"
    )
    status = "enough" if trace.final_sufficiency.enough else "insufficient"
    print(f"Sufficiency: {status}")
    if debug:
        _print_requirement_statuses(trace.final_sufficiency)
    print(f"Retries: {trace.retry_count}")
    teaching_status = (trace.teaching or {}).get("completion_status")
    if (trace.teaching or {}).get("delivery_path") == "full_direct_fallback":
        print("Delivery path: full_direct_fallback (Full 正文兜底)")
    if teaching_status in {"failed", "partial"}:
        print(f"Generation status: {teaching_status}; {(trace.teaching or {}).get('error')}")
    if not answer_already_published:
        print("\nAnswer:")
        print(result.answer_result.answer)
    if debug:
        _print_teaching_summary(trace.teaching)
        if getattr(trace, "agent_retrieval", None):
            from devcontext.tool_agent.trace import render_agent_trace
            print("\n" + render_agent_trace(trace.agent_retrieval))
        _print_sources(result)
        if not (trace.teaching or {}).get("request_policy"):
            print("\nTrace:")
            print(json.dumps(trace.to_dict(), ensure_ascii=False, indent=2))


def _print_requirement_statuses(sufficiency: SufficiencyResult) -> None:
    """Only the per-sub-question path fills statuses; the legacy path prints nothing.

    Prints the coverage state rather than a yes/no. Reducing PARTIAL, MISSING and
    UNVERIFIED to one word used to report "could not verify" as "missing", which
    is a different and much stronger claim.
    """
    if not sufficiency.statuses:
        return
    summary = ", ".join(
        f"{status.sub_question_id} {status.state or ('SATISFIED' if status.satisfied else 'MISSING')}"
        for status in sufficiency.statuses
    )
    print(f"Requirements: {summary}")


def _print_teaching_summary(teaching: dict | None) -> None:
    """The teach path's shape at a glance, instead of reading the whole trace."""
    if not teaching:
        return
    policy = teaching.get("request_policy")
    if policy:
        print(f"\nAnswer profile: {policy['profile']}; status: {teaching.get('completion_status')}; intent: {teaching.get('question_kind', policy['primary_intent'])}")
        print(f"Sections: {teaching.get('sections_emitted', 0)} / {teaching.get('sections_planned', 0)}")
        print(f"Delivery path: {teaching.get('delivery_path', 'full')}")
        if teaching.get("fallback_reason"):
            print(f"Fallback reason: {teaching['fallback_reason']}")
        print(f"Total request: {teaching.get('total_elapsed_ms', 0)/1000:.2f}s")
        return
    if teaching.get("generation_mode") == "v3":
        blueprint = teaching.get("blueprint") or {}
        print(f"\nV3 Teaching: {teaching.get('question_kind')}; {teaching.get('completion_status')}")
        print(f"Learning Goal: {blueprint.get('learning_goal', '(none)')}")
        for relation in blueprint.get("core_mental_model", []):
            print(f"  {relation['statement']}")
        print(f"Sections: {teaching.get('sections_emitted', 0)} / {teaching.get('sections_planned', 0)}")
        print(f"Total request: {teaching.get('total_elapsed_ms', 0) / 1000:.1f}s")
        return
    strategies = " + ".join(
        [teaching.get("primary_strategy") or ""]
        + list(teaching.get("secondary_strategies") or [])
    ).strip(" +")
    print(f"\nExplanation Strategy:\n{strategies}")
    print(f"\nMental Model:\n{teaching.get('core_mental_model') or '(none)'}")
    views = teaching.get("context_views") or {}
    print("\nContext:")
    print(f"  Workspace evidence: {views.get('workspace_evidence')}")
    print(f"  Bound evidence: {views.get('bound_evidence')}")
    for section_id, size in (views.get("per_section") or {}).items():
        print(f"  {section_id} view: {size}")
    drafts = teaching.get("section_drafts") or []
    revision = teaching.get("revision_trace") or {}
    issues = len(revision.get("global_issues") or []) + len(revision.get("section_issues") or [])
    print("\nGeneration:")
    print(f"  {len(drafts)} sections")
    print(f"  {len(revision.get('revision_required') or [])} targeted revision")
    if issues:
        print(f"  {issues} review issues")


def _print_sources(result: AgenticAnswerResult) -> None:
    print("\nSources:")
    citations = {
        item.citation.label: item.citation for item in result.context_bundle.items
    }
    if not result.answer_result.used_citations:
        print("(none)")
    else:
        for label in result.answer_result.used_citations:
            print(format_source(citations[label]))


def _print_question_plan(plan: QuestionPlan) -> None:
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))


def _print_evidence_plan(plan: EvidencePlan) -> None:
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))


def _v3_client_factory(settings: Settings, factory):
    def create():
        client = factory()
        if settings.llm_provider == "openai":
            client.reasoning_effort = settings.openai_v3_reasoning_effort
        return client
    return create


def _profile_answer_factory(settings: Settings, *, planner=False):
    return lambda: create_llm_client(settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=(settings.deepseek_answer_planner_model if planner else settings.deepseek_answer_model) or settings.deepseek_model,
        requested_reasoning_effort=settings.answer_reasoning_effort or ("low" if settings.answer_profile == "fast" else "high"),
        max_tokens=min(settings.model_capabilities().max_output_tokens, 32768 if settings.answer_profile == "full" else (8000 if planner else 16000)),
        timeout_seconds=(settings.answer_full_planner_timeout_seconds if planner else settings.answer_full_writer_timeout_seconds) if settings.answer_profile == "full" else (settings.answer_planner_timeout_seconds if planner else settings.answer_writer_timeout_seconds),
        json_mode=planner)


def _query_router(settings: Settings) -> QueryRouter:
    return QueryRouter(
        lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
            legacy_model=settings.deepseek_model,
            max_tokens=1024,
        )
    )


def _short_llm_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_model,
        max_tokens=1024,
    )


def _answer_client(settings: Settings, *, explain: bool = False) -> DeepSeekLLMClient:
    return create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
        reasoning_effort="high" if explain else "low",
        max_tokens=EXPLAIN_ANSWER_MAX_TOKENS if explain else ANSWER_MAX_TOKENS,
    )


def _planner_llm_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_planner_model or settings.deepseek_model,
        reasoning_effort="low" if settings.llm_provider == "openai" else "high",
        max_tokens=PLANNER_MAX_TOKENS,
        timeout_seconds=120 if settings.answer_engine_enabled else (35 if settings.llm_provider == "openai" else 120),
        json_mode=True,
        **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
    )


def _rewrite_llm_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_model,
        reasoning_effort="low",
        max_tokens=REWRITE_MAX_TOKENS,
        json_mode=True,
        **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
    )


def _sub_question_sufficiency_factory(
    settings: Settings,
) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_model,
        reasoning_effort="low",
        max_tokens=SUB_QUESTION_SUFFICIENCY_MAX_TOKENS,
        json_mode=True,
        **({"requested_reasoning_effort": "low"} if settings.answer_engine_enabled else {}),
    )


def _answer_planner_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_answer_planner_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=ANSWER_PLANNER_MAX_TOKENS,
        json_mode=True,
    )


def _reviewer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_reviewer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=REVIEWER_MAX_TOKENS,
        json_mode=True,
    )


def _explanation_planner_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_answer_planner_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=EXPLANATION_PLANNER_MAX_TOKENS,
        json_mode=True,
    )


def _teaching_writer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=TEACHING_ANSWER_MAX_TOKENS,
    )


def _teaching_reviewer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_reviewer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=TEACHING_REVIEWER_MAX_TOKENS,
        json_mode=True,
    )


def _composer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        legacy_model=settings.deepseek_answer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=COMPOSER_MAX_TOKENS,
    )


def _question_planner(settings: Settings) -> QuestionPlanner:
    return QuestionPlanner(_planner_llm_factory(settings))


def _evidence_planner(settings: Settings) -> EvidencePlanner:
    return EvidencePlanner(_planner_llm_factory(settings), full_teaching=settings.answer_engine_enabled and settings.answer_profile == "full")


def _answer_judge_factory(
    settings: Settings, model: str | None = None
) -> Callable[[], LLMClient]:
    """The blind judge reads two long answers and emits a tiny verdict."""
    return lambda: create_llm_client(
        settings, deepseek_client_type=DeepSeekLLMClient,
        model=model,
        reasoning_effort="high",
        max_tokens=JUDGE_MAX_TOKENS,
        json_mode=True,
    )


def _agentic_workflow(
    settings: Settings,
    builder: ContextBuilder,
) -> AgenticRetrievalWorkflow:
    short_llm_factory = _short_llm_factory(settings)
    return AgenticRetrievalWorkflow(
        router=_query_router(settings),
        retrieval_policy=RetrievalPolicy(RetrievalService(settings)),
        context_builder=builder,
        sufficiency_checker=ContextSufficiencyChecker(short_llm_factory),
        query_rewriter=TargetedQueryRewriter(_rewrite_llm_factory(settings)),
        answer_generator_factory=lambda: AnswerGenerator(_answer_client(settings)),
    )


def _planned_workflow(
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
    controller = _retrieval_controller(settings)
    teaching_workflow = None
    if answer_mode == "teach":
        # The teach path keeps its own client policy: its planner and its writer
        # do different work from the explain path's, so sharing budgets and
        # effort settings between them would just constrain both.
        teaching_workflow = TeachingExplanationWorkflow(
            ExplanationPlanner(_explanation_planner_factory(settings)),
            TeachingWriter(_teaching_writer_factory(settings)),
            SectionComposer(_composer_factory(settings)),
            TeachingReviewer(_teaching_reviewer_factory(settings)),
            capabilities=settings.model_capabilities(),
            runtime_options=TeachingRuntimeOptions(settings.teaching_generation_mode),
            micro_planner=MicroExplanationPlanner(_profile_answer_factory(settings, planner=True) if request_policy else _explanation_planner_factory(settings), settings.model_capabilities()),
            streaming_writer=SingleStreamingTeachingWriter(_profile_answer_factory(settings) if request_policy else _teaching_writer_factory(settings), permissive=request_policy is not None),
        )
        if settings.teaching_generation_mode == "v3" or (request_policy and request_policy.profile == "full"):
            from devcontext.explanation.v3.planner import TeachingPlannerV3
            from devcontext.explanation.v3.writer import TeachingWriterV3
            teaching_workflow.request_timeout_seconds = settings.teaching_request_timeout_seconds
            teaching_workflow.v3_planner = TeachingPlannerV3(
                _profile_answer_factory(settings, planner=True) if request_policy else _v3_client_factory(settings, _explanation_planner_factory(settings)), teaching_workflow.capabilities, teaching_workflow.estimator)
            teaching_workflow.v3_writer = TeachingWriterV3(_profile_answer_factory(settings) if request_policy else _v3_client_factory(settings, _teaching_writer_factory(settings)), permissive=request_policy is not None)
    return EvidenceDrivenWorkflow(
        controller,
        answer_generator_factory=lambda: AnswerGenerator(
            _answer_client(settings, explain=answer_mode == "explain")
        ),
        answer_planner=AnswerPlanner(_answer_planner_factory(settings)),
        answer_reviewer=AnswerReviewer(_reviewer_factory(settings)),
        answer_mode=answer_mode,
        context_budget_override=context_budget_override,
        teaching_workflow=teaching_workflow,
        request_policy=request_policy,
    )


def _retrieval_controller(
    settings: Settings,
    *,
    observer: RetrievalObserver | None = None,
    frozen_case: dict | None = None,
    frozen_plan: EvidencePlan | None = None,
    shared_coverage_factory=None,
) -> RetrievalEngine:
    source_policy = SourcePolicy.from_file(
        settings.source_policy_path or default_source_policy_path()
    )
    if frozen_case is None:
        evidence_planner = _evidence_planner(settings)
        action_planner = SearchActionPlanner(_rewrite_llm_factory(settings))
        coverage_checker = CoverageChecker(
            _sub_question_sufficiency_factory(settings)
        )
        if settings.answer_engine_enabled and settings.answer_profile == "fast" and frozen_plan is None:
            from devcontext.agentic.fast import FastRetrievalPlanner, FastActionPlanner, FastCoverageChecker
            evidence_planner = FastRetrievalPlanner(_planner_llm_factory(settings))
            action_planner = FastActionPlanner(evidence_planner)
            if not settings.tool_agent_enabled and shared_coverage_factory is None:
                coverage_checker = FastCoverageChecker(CoverageChecker(_sub_question_sufficiency_factory(settings), max_attempts=1), evidence_planner)
    else:
        evidence_planner = FrozenEvidencePlanner(frozen_case)
        action_planner = FrozenSearchActionPlanner(frozen_case)
        coverage_checker = OracleCoverageChecker(frozen_case)
    if frozen_plan is not None:
        from devcontext.agentic.retrieval_engine import FixedEvidencePlanner
        evidence_planner = FixedEvidencePlanner(frozen_plan)
    if shared_coverage_factory is not None:
        coverage_checker = shared_coverage_factory()
    if settings.tool_agent_enabled:
        from devcontext.agentic.tool_driven_retrieval_controller import ToolDrivenRetrievalController
        from devcontext.code_graph.store import CodeGraphStore
        from devcontext.tool_agent.executor import ToolExecutor
        from devcontext.tool_agent.planner import AgentPlanner
        from devcontext.tool_agent.registry import ToolRegistry
        from devcontext.tool_agent.runtime import AgentRuntime
        from devcontext.tool_agent.tools import RepositoryTools
        if shared_coverage_factory is None and frozen_case is None:
            coverage_checker = CoverageChecker(_sub_question_sufficiency_factory(settings), max_attempts=1)
        tools = RepositoryTools(RetrievalPolicy(RetrievalService(settings)),
            CodeGraphStore(settings.database_url, settings.repository_name, settings.symbol_graph_query_timeout_seconds), observer)
        factory = lambda: create_llm_client(settings, deepseek_client_type=DeepSeekLLMClient,
            legacy_model=settings.deepseek_planner_model or settings.deepseek_model,
            requested_reasoning_effort="low", max_tokens=8192, json_mode=True,
            timeout_seconds=settings.tool_agent_planner_timeout_seconds)
        return ToolDrivenRetrievalController(evidence_planner,
            AgentRuntime(AgentPlanner(factory), ToolExecutor(ToolRegistry(tools)), coverage_checker, source_policy), observer)
    return RetrievalController(
        evidence_planner=evidence_planner,
        action_planner=action_planner,
        retrieval_policy=RetrievalPolicy(RetrievalService(settings)),
        coverage_checker=coverage_checker,
        source_policy=source_policy,
        observer=observer,
        graph_expander=_code_graph_expander(settings),
    )


def _code_graph_expander(settings: Settings):
    if not settings.symbol_graph_enabled:
        return None
    from devcontext.code_graph.expansion import CodeGraphExpander
    from devcontext.code_graph.store import CodeGraphStore
    return CodeGraphExpander(CodeGraphStore(settings.database_url, settings.repository_name,
                                           settings.symbol_graph_query_timeout_seconds))


def _budget(args: argparse.Namespace, *, planned: bool) -> tuple[int, int]:
    default_top_k = PLANNED_TOP_K if planned else LEGACY_TOP_K
    default_max_chars = PLANNED_MAX_CHARS if planned else LEGACY_MAX_CHARS
    top_k = args.top_k if args.top_k is not None else default_top_k
    max_chars = args.max_chars if args.max_chars is not None else default_max_chars
    return top_k, max_chars


def _routed_search(
    settings: Settings, query: str, top_k: int
) -> tuple[RouteDecision, list[SearchResult]]:
    decision = _query_router(settings).route(query)
    policy = RetrievalPolicy(RetrievalService(settings))
    return decision, policy.search(query, decision, top_k)


def _run_ask(
    run_once: Callable[[], AgenticAnswerResult],
    *,
    args: argparse.Namespace,
    top_k: int,
    answer_mode: str,
) -> tuple[AgenticAnswerResult, list[dict]]:
    """Answer, optionally repeated, with a trace collected for each sample.

    Repetitions share this interpreter, so later samples skip a fresh process
    start. They are still ``repeat_sample`` and not "warm": a standalone ask
    shares no client, connection or cache across processes either, so calling
    the Nth run warm would overstate what changed.

    With no performance flags and a single run, nothing is recorded at all and
    the command behaves exactly as it did before tracing existed.
    """
    repeats = max(1, args.repeat)
    profiling = bool(args.perf) or args.perf_json is not None or repeats > 1 or (args.debug and getattr(args, "_answer_engine_enabled", False))
    if not profiling:
        return run_once(), []

    reports: list[dict] = []
    result: AgenticAnswerResult | None = None
    for index in range(repeats):
        recorder = PerfRecorder()
        started = time.perf_counter()
        with capture(recorder):
            result = run_once()
        reports.append(
            build_perf_report(
                recorder=recorder,
                result=result,
                query=args.query,
                answer_mode=answer_mode,
                depth=None,
                top_k=top_k,
                wall_clock_ms=(time.perf_counter() - started) * 1000,
                sample_kind="first_sample" if index == 0 else "repeat_sample",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
    assert result is not None
    return result, reports


def _emit_perf(args: argparse.Namespace, reports: list[dict]) -> None:
    if not reports:
        return
    # Repeating without asking for a summary still prints one: N identical
    # answers with no numbers would be useless.
    if args.perf or len(reports) > 1 or (args.debug and getattr(args, "_answer_engine_enabled", False)):
        for report in reports:
            print()
            print(render_summary(report))
            if args.debug:
                policy = (report.get("stream") or {}).get("request_policy") or {}
                if policy:
                    print(f"Profile: {policy['profile']}; intent: {(report.get('stream') or {}).get('question_kind', policy['primary_intent'])}")
                    print(f"Reasoning requested: {policy['reasoning_effort']}; hard timeout: {policy['hard_timeout_seconds']}")
                    print(f"Latency target: {policy['latency_target_seconds']}s; exceeded: {(report.get('stream') or {}).get('latency_target_exceeded')}")
                for call in report['llm_calls']:
                    requested = call.get('requested_reasoning_effort') or call.get('reasoning_effort') or 'unspecified'
                    sent = call.get('reasoning_effort') or 'omitted (model default)'
                    print(f"  {call['stage']}: {call.get('model')}; requested={requested}; sent={sent}; {call['latency_ms']/1000:.2f}s")
                first = (report.get('stream') or {}).get('first_section_ready_ms')
                if first is not None:
                    print(f"First section: {first/1000:.2f}s from request entry")
    if args.perf_json is not None:
        payload: object = reports[0] if len(reports) == 1 else {"samples": reports}
        args.perf_json.parent.mkdir(parents=True, exist_ok=True)
        args.perf_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Performance report written to {args.perf_json}")


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    supplied = sys.argv[1:] if argv is None else argv
    if any(arg == "--depth" or arg.startswith("--depth=") for arg in supplied):
        _parser().error("--depth 已移除，请直接在问题中表达简要或详细；回答按理解任务展开。")
    args = _parser().parse_args(argv)
    settings = Settings()
    try:
        request_policy = None
        if args.command == "ask":
            from devcontext.answer_policy import resolve_policy
            mapping = {"single_stream": "fast", "v3": "full"}
            engine = args.teaching_generation_mode
            new_controls = args.profile is not None or args.reasoning_effort is not None
            if new_controls and (args.answer_mode != "teach" or engine == "multi_pass" or args.no_plan or args.plan_only):
                raise ValueError("--profile/--reasoning-effort conflict with legacy/explain/multi_pass or plan-only/no-plan")
            if engine in mapping and args.profile is not None and mapping[engine] != args.profile:
                raise ValueError("--profile conflicts with --teaching-generation-mode")
            if args.answer_mode == "teach" and engine != "multi_pass" and not args.no_plan and not args.plan_only:
                request_policy = resolve_policy(args.query, settings, profile=args.profile or mapping.get(engine),
                    reasoning_effort=args.reasoning_effort, hard_timeout=args.hard_timeout)
                settings = settings.model_copy(update={"answer_engine_enabled": True, "answer_profile": request_policy.profile,
                    "answer_reasoning_effort": request_policy.reasoning_effort,
                    "answer_request_policy": request_policy,
                    "teaching_generation_mode": "single_stream" if request_policy.profile == "fast" else "v3"})
                args._answer_engine_enabled = True
            elif args.hard_timeout is not None:
                raise ValueError("--hard-timeout requires the Fast/Full answering workflow")
        if args.command == "ask" and args.teaching_generation_mode is not None:
            if args.answer_mode != "teach" or args.no_plan or args.plan_only:
                raise ValueError("--teaching-generation-mode requires the teach answering workflow")
            settings = settings.model_copy(update={"teaching_generation_mode": args.teaching_generation_mode})
        if args.command == "init-db":
            ChunkStore(settings.database_url).initialize()
            print("Database schema is ready.")
        elif args.command == "smoke-api":
            client = BailianEmbeddingClient(
                api_key=settings.api_key(),
                base_url=settings.dashscope_base_url,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
                transport=settings.embedding_transport,
            )
            vector = client.embed_query("DevContext connectivity check")
            print(f"Bailian embedding is ready: model={settings.embedding_model} dimensions={len(vector)}")
        elif args.command == "ingest":
            summary = ingest(settings)
            print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
        elif args.command == "evaluate-tool-agent":
            from devcontext.evaluation.tool_agent_runner import load_tool_cases, run_paired_evaluation, corpus_hash
            paths = args.cases or [project_root() / "benchmark" / "tool-agent-v1.jsonl"]
            cases = [case for path in paths for case in load_tool_cases(path)]
            if len({case["id"] for case in cases}) != len(cases):
                raise ValueError("Case ids must be unique across suites")
            if args.limit is not None:
                if args.limit < 1:
                    raise ValueError("limit must be positive")
                cases = cases[:args.limit]
            def factory(arm, case, planner, checker_factory, observer):
                arm_settings = settings.model_copy(update={"tool_agent_enabled": arm == "tool_agent",
                    "symbol_graph_enabled": arm == "auto_graph"})
                return _retrieval_controller(arm_settings, observer=observer, frozen_plan=planner.frozen_plan,
                    frozen_case=case if args.checker == "oracle" else None,
                    shared_coverage_factory=checker_factory)
            def checker_factory(case):
                return OracleCoverageChecker(case) if args.checker == "oracle" else CoverageChecker(
                    _sub_question_sufficiency_factory(settings), max_attempts=1)
            output = args.output or project_root() / "artifacts" / "tool-agent-v1-report.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            def checkpoint(report):
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            def result_lookup(ids):
                from devcontext.code_graph.store import CodeGraphStore
                with CodeGraphStore(settings.database_url, settings.repository_name).session() as session:
                    return session.chunks_for_symbols([{"chunk_id": i} for i in ids])
            report = run_paired_evaluation(cases, factory, checker_factory, runs=args.runs, top_k=args.top_k,
                progress=lambda text: print(text, flush=True), corpus_fingerprint=lambda: corpus_hash(settings), checkpoint=checkpoint,
                result_lookup=result_lookup)
            report["checker_configuration"] = {"kind": args.checker, "max_attempts": 1 if args.checker == "semantic" else None,
                                               "model": settings.text_model() if args.checker == "semantic" else None}
            report["datasets"] = [{"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()} for path in paths]
            checkpoint(report)
            print(json.dumps({"output": str(output), "summary": report["summary"], "acceptance": report["acceptance"]}, ensure_ascii=False, indent=2))
        elif args.command == "evaluate-symbol-graph":
            from devcontext.evaluation.symbol_graph_runner import run_fixture_evaluation, run_graph_evaluation, load_graph_cases
            from devcontext.code_graph.store import CodeGraphStore
            if args.fixture:
                if args.cases:
                    raise ValueError("--fixture uses the bundled source benchmark; omit --cases")
                report = run_fixture_evaluation(settings)
            else:
                if args.cases is None:
                    raise ValueError("Use --fixture or provide --cases matching the indexed repository")
                report = run_graph_evaluation(CodeGraphStore(settings.database_url, settings.repository_name,
                    settings.symbol_graph_query_timeout_seconds), load_graph_cases(args.cases))
            output = args.output or project_root() / "artifacts" / "symbol-graph-v1-report.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(json.dumps({"output": str(output), "summary": report["summary"], "acceptance": report["acceptance"]}, ensure_ascii=False, indent=2))
        elif args.command == "graph":
            from devcontext.code_graph.query import CodeGraphQuery
            from devcontext.code_graph.store import CodeGraphStore
            result = CodeGraphQuery(CodeGraphStore(settings.database_url, args.repository or settings.repository_name,
                                                   settings.symbol_graph_query_timeout_seconds)).query(args.operation, args.value)
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        elif args.command == "search":
            results = RetrievalService(settings).search(args.strategy, args.query, args.top_k)
            _print_results(results, args.format)
        elif args.command == "context":
            builder = ContextBuilder(max_chars=args.max_chars)
            decision, results = _routed_search(settings, args.query, args.top_k)
            bundle = builder.build(args.query, results)
            print(f"Query: {bundle.query}")
            print(f"Route: {decision.query_type.value} ({decision.decision_source.value})")
            if bundle.rendered_text:
                print()
                print(bundle.rendered_text)
            else:
                print("No context found.")
        elif args.command == "ask":
            if args.plan_only:
                if args.plan_format == "legacy":
                    _print_question_plan(_question_planner(settings).plan(args.query))
                else:
                    _print_evidence_plan(_evidence_planner(settings).plan(args.query))
            elif args.no_plan:
                top_k, max_chars = _budget(args, planned=False)

                def run_legacy() -> AgenticAnswerResult:
                    return _agentic_workflow(
                        settings, ContextBuilder(max_chars=max_chars)
                    ).run(args.query, top_k)

                result, reports = _run_ask(
                    run_legacy, args=args, top_k=top_k, answer_mode="legacy"
                )
                _print_agentic_answer(args.query, result, debug=args.debug)
                _emit_perf(args, reports)
            else:
                top_k, max_chars = _budget(args, planned=True)
                legacy_top_k, legacy_max_chars = _budget(args, planned=False)

                streamed_sections = []

                def publish_section(section):
                    if not streamed_sections:
                        print("\nAnswer:", flush=True)
                    print(section.markdown + "\n", flush=True)
                    streamed_sections.append(section)

                def run_planned() -> AgenticAnswerResult:
                    # Rebuilt per repetition: samples share an interpreter, not
                    # mutable workflow state.
                    workflow = _planned_workflow(
                        settings,
                        ContextBuilder(max_chars=max_chars),
                        ContextBuilder(max_chars=legacy_max_chars),
                        args.answer_mode,
                        args.max_chars,
                    )
                    if request_policy is not None or (settings.teaching_generation_mode == "v3" and args.answer_mode == "teach"):
                        return workflow.run(args.query, top_k, on_section=publish_section)
                    return workflow.run(args.query, top_k)

                result, reports = _run_ask(
                    run_planned, args=args, top_k=top_k,
                    answer_mode=args.answer_mode,
                )
                _print_agentic_answer(
                    args.query, result, debug=args.debug, include_plan=True,
                    answer_already_published=bool(streamed_sections),
                )
                _emit_perf(args, reports)
                teaching_trace = getattr(getattr(result, "trace", None), "teaching", None) or {}
                if teaching_trace.get("generation_mode") in {"v3", "single_stream"} and teaching_trace.get("completion_status") != "complete":
                    return 1
        elif args.command == "evaluate":
            report = evaluate(settings, benchmark=args.benchmark, baseline=args.baseline)
            summary = [
                {key: value for key, value in strategy.items() if key != "cases"}
                for strategy in report["strategies"]
            ]
            routing_summary = {
                key: value for key, value in report["routing"].items() if key != "cases"
            }
            print(
                json.dumps(
                    {
                        "output": report["output"],
                        "benchmark_sha256": report["benchmark_sha256"],
                        "baseline_comparison": report["baseline_comparison"],
                        "acceptance": report["acceptance"],
                        "quality_passed": report["quality_passed"],
                        "routing": routing_summary,
                        "policy_comparison": report["policy_comparison"],
                        "policy_acceptance": report["policy_acceptance"],
                        "policy_improvement_passed": report[
                            "policy_improvement_passed"
                        ],
                        "bottleneck_analysis": {
                            key: value
                            for key, value in report["bottleneck_analysis"].items()
                            if key != "cases"
                        },
                        "strategies": summary,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "evaluate-retrieval-workflow":
            if args.cases is not None and args.suite == "all":
                raise ValueError("--cases requires --suite l1.5 or --suite regression")
            if args.mode == "frozen" and args.runs != 1:
                raise ValueError("--runs greater than 1 requires live or both mode")
            benchmark_root = project_root() / "benchmark"
            case_sets: dict[str, tuple[Path, bool]] = {}
            if args.suite in {"l1.5", "all"}:
                case_sets["l1.5"] = (
                    args.cases if args.suite == "l1.5" and args.cases else
                    benchmark_root / "l1.5-retrieval.jsonl",
                    False,
                )
            if args.suite in {"regression", "all"}:
                case_sets["regression"] = (
                    args.cases if args.suite == "regression" and args.cases else
                    benchmark_root / "regression" / "regression-v1.jsonl",
                    True,
                )
            modes = {
                "frozen": ("frozen",),
                "live": ("live",),
                "both": ("frozen", "live"),
            }[args.mode]

            def retrieval_controller_factory(
                case: dict, mode: str, observer: RetrievalTraceRecorder
            ) -> RetrievalController:
                return _retrieval_controller(
                    settings.model_copy(update={"symbol_graph_enabled": False}) if args.symbol_graph_ab else settings,
                    observer=observer,
                    frozen_case=case if mode == "frozen" else None,
                )

            source_policy_path = (
                settings.source_policy_path or default_source_policy_path()
            )
            report = run_retrieval_workflow_evaluation(
                case_sets=case_sets,
                modes=modes,
                runs=args.runs,
                controller_factory=retrieval_controller_factory,
                only=(
                    [item for item in args.only.split(",") if item]
                    if args.only else None
                ),
                limit=args.limit,
                source_policy_path=source_policy_path,
                progress=lambda message: print(message, flush=True),
            )
            if args.symbol_graph_ab:
                from devcontext.evaluation.symbol_graph_runner import compare_workflow_graph_ab
                graph_settings = settings.model_copy(update={"symbol_graph_enabled": True})
                def graph_controller_factory(case, mode, observer):
                    return _retrieval_controller(graph_settings, observer=observer,
                                                 frozen_case=case if mode == "frozen" else None)
                graph_report = run_retrieval_workflow_evaluation(
                    case_sets=case_sets, modes=modes, runs=args.runs,
                    controller_factory=graph_controller_factory,
                    only=[item for item in args.only.split(",") if item] if args.only else None,
                    limit=args.limit, source_policy_path=source_policy_path,
                    progress=lambda message: print(message, flush=True),
                )
                report["symbol_graph_ab"] = {"off": report["summary"], "on": graph_report,
                    "comparison": compare_workflow_graph_ab(report, graph_report),
                    "note": "Same strategy, budgets and initial cases; live planner/coverage calls may vary. Graph traces distinguish direct additions from follow-up changes."}
            baseline_path = (
                args.baseline or benchmark_root / "baselines" /
                "retrieval-workflow-v1.json"
            )
            if baseline_path.is_file():
                baseline_value = json.loads(baseline_path.read_text(encoding="utf-8"))
                report["baseline_comparison"] = compare_workflow_baseline(
                    report, baseline_value
                )
            else:
                report["baseline_comparison"] = {
                    "status": "missing", "path": str(baseline_path)
                }
            artifacts = project_root() / "artifacts"
            artifacts.mkdir(exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            output = args.output or artifacts / f"retrieval-workflow-{timestamp}.json"
            report["output"] = str(output)
            output.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if args.write_baseline:
                baseline_value = build_workflow_baseline(report)
                args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
                args.write_baseline.write_text(
                    json.dumps(baseline_value, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "summary": report["summary"],
                        "acceptance": report["acceptance"],
                        "quality_passed": report["quality_passed"],
                        "baseline_comparison": report["baseline_comparison"],
                        **({"symbol_graph_ab": report["symbol_graph_ab"]["comparison"]} if args.symbol_graph_ab else {}),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.command == "evaluate-answers":
            cases_path = args.cases or project_root() / "benchmark" / "l2-answer-quality.jsonl"
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            artifacts = project_root() / "artifacts"
            artifacts.mkdir(exist_ok=True)
            output = args.output or artifacts / f"l2-answer-quality-{timestamp}.json"
            # A stable journal so an interrupted run can be completed with --resume.
            journal = artifacts / "l2-answer-quality-journal.jsonl"

            def workflow_factory(mode: str, depth: str) -> EvidenceDrivenWorkflow:
                # No explicit Context override: EvidencePlan complexity determines
                # the retrieval budget; forced depth affects presentation only.
                return _planned_workflow(
                    settings,
                    ContextBuilder(max_chars=PLANNED_MAX_CHARS),
                    ContextBuilder(max_chars=LEGACY_MAX_CHARS),
                    mode,
                    None,
                )

            outcome = run_answer_quality_evaluation(
                cases_path=cases_path,
                workflow_factory=workflow_factory,
                judge_client_factory=_answer_judge_factory(settings, args.judge_model),
                pedagogy_judge_factory=_answer_judge_factory(settings, args.judge_model),
                config=PairwiseEvaluationConfig(
                    args.baseline_mode, args.candidate_mode
                ),
                only=[item for item in args.only.split(",") if item] if args.only else None,
                limit=args.limit,
                journal_path=journal,
                resume=args.resume,
                progress=lambda message: print(message, flush=True),
            )
            summary = outcome["summary"]
            output.write_text(
                json.dumps(
                    {"summary": summary, "records": outcome["records"]},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "output": str(output),
                        "judge_winners": summary["judge_winners"],
                        "arms": summary["arms"],
                        "acceptance": summary["acceptance"],
                        "human_review_queue": summary["human_review_queue"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    except Exception as exception:
        print(f"ERROR: {exception}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
