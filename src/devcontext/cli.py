from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from devcontext.agentic import (
    AgenticAnswerResult,
    AgenticRetrievalWorkflow,
    ContextSufficiencyChecker,
    PlannedRetrievalWorkflow,
    TargetedQueryRewriter,
)
from devcontext.answer import ANSWER_MAX_TOKENS, AnswerGenerator, format_source
from devcontext.config import Settings
from devcontext.context import ContextBuilder
from devcontext.embedding.client import BailianEmbeddingClient
from devcontext.evaluation.runner import evaluate
from devcontext.ingestion.pipeline import ingest
from devcontext.llm import DeepSeekLLMClient, LLMClient
from devcontext.models import SearchResult
from devcontext.planning import (
    EvidencePlan,
    EvidencePlanner,
    QuestionPlan,
    QuestionPlanner,
)
from devcontext.retrieval import RetrievalPolicy, RetrievalService
from devcontext.routing import QueryRouter, RouteDecision
from devcontext.storage import ChunkStore

LEGACY_TOP_K = 5
LEGACY_MAX_CHARS = 6000
PLANNED_TOP_K = 12
PLANNED_MAX_CHARS = 10000

# Planning needs more headroom than the other short calls: "why not" questions make
# the model reason long enough that reasoning tokens exhaust a 1024 budget before the
# JSON is emitted, which surfaces as finish_reason=length and silently falls back.
PLANNER_MAX_TOKENS = 1536


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
    ask.add_argument("--no-plan", action="store_true", help="Skip question planning and use the legacy single-query workflow")
    ask.add_argument("--plan-only", action="store_true", help="Print the question plan as JSON without retrieving or answering")
    ask.add_argument("--debug", action="store_true")

    evaluation = subparsers.add_parser("evaluate", help="Run the retrieval benchmark")
    evaluation.add_argument("--benchmark", type=Path)
    evaluation.add_argument("--baseline", type=Path)
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
    if include_plan and trace.plan:
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
    print(f"Retries: {trace.retry_count}")
    print("\nAnswer:")
    print(result.answer_result.answer)
    if debug:
        _print_sources(result)
        print("\nTrace:")
        print(json.dumps(trace.to_dict(), ensure_ascii=False, indent=2))


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


def _print_question_plan(plan: QuestionPlan, evidence_plan: EvidencePlan) -> None:
    # Merged rather than nested so the existing question-plan keys stay at the top level.
    payload = plan.to_dict()
    payload["evidence_plan"] = evidence_plan.to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2))


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


def _answer_client(settings: Settings) -> DeepSeekLLMClient:
    return DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        max_tokens=ANSWER_MAX_TOKENS,
    )


def _planner_llm_factory(settings: Settings) -> Callable[[], LLMClient]:
    return lambda: DeepSeekLLMClient(
        api_key=settings.deepseek_key(),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        max_tokens=PLANNER_MAX_TOKENS,
    )


def _question_planner(settings: Settings) -> QuestionPlanner:
    return QuestionPlanner(_planner_llm_factory(settings))


def _evidence_planner(settings: Settings) -> EvidencePlanner:
    # Same budget as the question planner: both emit short structured planning output.
    return EvidencePlanner(_planner_llm_factory(settings))


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
        query_rewriter=TargetedQueryRewriter(short_llm_factory),
        answer_generator_factory=lambda: AnswerGenerator(_answer_client(settings)),
    )


def _planned_workflow(
    settings: Settings,
    planned_builder: ContextBuilder,
    legacy_builder: ContextBuilder,
) -> PlannedRetrievalWorkflow:
    short_llm_factory = _short_llm_factory(settings)
    return PlannedRetrievalWorkflow(
        planner=_question_planner(settings),
        evidence_planner=_evidence_planner(settings),
        router=_query_router(settings),
        retrieval_policy=RetrievalPolicy(RetrievalService(settings)),
        context_builder=planned_builder,
        sufficiency_checker=ContextSufficiencyChecker(short_llm_factory),
        query_rewriter=TargetedQueryRewriter(short_llm_factory),
        legacy_workflow=_agentic_workflow(settings, legacy_builder),
        answer_generator_factory=lambda: AnswerGenerator(_answer_client(settings)),
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
                plan = _question_planner(settings).plan(args.query)
                _print_question_plan(plan, _evidence_planner(settings).plan(plan))
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
        return 0
    except Exception as exception:
        print(f"ERROR: {exception}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
