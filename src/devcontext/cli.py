from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from devcontext.agentic import (
    AgenticAnswerResult,
    AgenticRetrievalWorkflow,
    ContextSufficiencyChecker,
    CoverageChecker,
    EvidenceDrivenWorkflow,
    PlannedRetrievalWorkflow,
    RetrievalController,
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
from devcontext.planning import EvidencePlan, EvidencePlanner, QuestionPlan, QuestionPlanner
from devcontext.request import ANSWER_MODES, TEACH_DEPTHS
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
        "--depth",
        choices=TEACH_DEPTHS,
        default=None,
        help=(
            "Force presentation depth without changing evidence retrieval. "
            "'deep' is only available with --answer-mode teach."
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
        print(f"Depth: {trace.plan['answer_depth']}")
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
    print("\nAnswer:")
    print(result.answer_result.answer)
    if debug:
        _print_teaching_summary(trace.teaching)
        _print_sources(result)
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


def _query_router(settings: Settings) -> QueryRouter:
    return QueryRouter(
        lambda: DeepSeekLLMClient(
            api_key=settings.deepseek_key(),
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            max_tokens=1024,
        )
    )


def _short_llm_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        max_tokens=1024,
    )


def _answer_client(settings: Settings, *, explain: bool = False) -> DeepSeekLLMClient:
    return DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_answer_model or settings.deepseek_model,
        reasoning_effort="high" if explain else "low",
        max_tokens=EXPLAIN_ANSWER_MAX_TOKENS if explain else ANSWER_MAX_TOKENS,
    )


def _planner_llm_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_planner_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=PLANNER_MAX_TOKENS,
        json_mode=True,
    )


def _rewrite_llm_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        reasoning_effort="low",
        max_tokens=REWRITE_MAX_TOKENS,
        json_mode=True,
    )


def _sub_question_sufficiency_factory(
    settings: Settings,
) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        reasoning_effort="low",
        max_tokens=SUB_QUESTION_SUFFICIENCY_MAX_TOKENS,
        json_mode=True,
    )


def _answer_planner_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_answer_planner_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=ANSWER_PLANNER_MAX_TOKENS,
        json_mode=True,
    )


def _reviewer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_reviewer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=REVIEWER_MAX_TOKENS,
        json_mode=True,
    )


def _explanation_planner_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_answer_planner_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=EXPLANATION_PLANNER_MAX_TOKENS,
        json_mode=True,
    )


def _teaching_writer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_answer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=TEACHING_ANSWER_MAX_TOKENS,
    )


def _teaching_reviewer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_reviewer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=TEACHING_REVIEWER_MAX_TOKENS,
        json_mode=True,
    )


def _composer_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_answer_model or settings.deepseek_model,
        reasoning_effort="high",
        max_tokens=COMPOSER_MAX_TOKENS,
    )


def _question_planner(settings: Settings) -> QuestionPlanner:
    return QuestionPlanner(_planner_llm_factory(settings))


def _evidence_planner(settings: Settings) -> EvidencePlanner:
    return EvidencePlanner(_planner_llm_factory(settings))


def _answer_judge_factory(
    settings: Settings, model: str | None = None
) -> Callable[[], LLMClient]:
    """The blind judge reads two long answers and emits a tiny verdict."""
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=model or settings.deepseek_model,
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
    depth_override: str | None = None,
) -> EvidenceDrivenWorkflow:
    # Keep the builder arguments for one-cycle factory compatibility.  The new
    # controller derives its budget from EvidencePlan complexity unless the
    # caller explicitly supplies context_budget_override.
    _ = planned_builder, legacy_builder
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
        )
    return EvidenceDrivenWorkflow(
        controller,
        answer_generator_factory=lambda: AnswerGenerator(
            _answer_client(settings, explain=answer_mode == "explain")
        ),
        answer_planner=AnswerPlanner(_answer_planner_factory(settings)),
        answer_reviewer=AnswerReviewer(_reviewer_factory(settings)),
        answer_mode=answer_mode,
        context_budget_override=context_budget_override,
        depth_override=depth_override,
        teaching_workflow=teaching_workflow,
    )


def _retrieval_controller(
    settings: Settings,
    *,
    observer: RetrievalObserver | None = None,
    frozen_case: dict | None = None,
) -> RetrievalController:
    source_policy = SourcePolicy.from_file(
        settings.source_policy_path or default_source_policy_path()
    )
    if frozen_case is None:
        evidence_planner = _evidence_planner(settings)
        action_planner = SearchActionPlanner(_rewrite_llm_factory(settings))
        coverage_checker = CoverageChecker(
            _sub_question_sufficiency_factory(settings)
        )
    else:
        evidence_planner = FrozenEvidencePlanner(frozen_case)
        action_planner = FrozenSearchActionPlanner(frozen_case)
        coverage_checker = OracleCoverageChecker(frozen_case)
    return RetrievalController(
        evidence_planner=evidence_planner,
        action_planner=action_planner,
        retrieval_policy=RetrievalPolicy(RetrievalService(settings)),
        coverage_checker=coverage_checker,
        source_policy=source_policy,
        observer=observer,
    )


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


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    settings = Settings()
    try:
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
                result = _agentic_workflow(
                    settings, ContextBuilder(max_chars=max_chars)
                ).run(args.query, top_k)
                _print_agentic_answer(args.query, result, debug=args.debug)
            else:
                top_k, max_chars = _budget(args, planned=True)
                legacy_top_k, legacy_max_chars = _budget(args, planned=False)
                workflow = _planned_workflow(
                    settings,
                    ContextBuilder(max_chars=max_chars),
                    ContextBuilder(max_chars=legacy_max_chars),
                    args.answer_mode,
                    args.max_chars,
                    args.depth,
                )
                result = workflow.run(args.query, top_k)
                _print_agentic_answer(
                    args.query, result, debug=args.debug, include_plan=True
                )
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
                    settings,
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
                    depth,
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
