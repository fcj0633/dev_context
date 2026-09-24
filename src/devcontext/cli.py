from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from devcontext.answer import AnswerGenerator, EMPTY_CONTEXT_ANSWER, format_source
from devcontext.config import Settings
from devcontext.context import ContextBuilder
from devcontext.embedding.client import BailianEmbeddingClient
from devcontext.evaluation.runner import evaluate
from devcontext.ingestion.pipeline import ingest
from devcontext.llm import DeepSeekLLMClient
from devcontext.models import AnswerResult, ContextBundle, SearchResult
from devcontext.retrieval import RetrievalPolicy, RetrievalService
from devcontext.routing import QueryRouter, RouteDecision
from devcontext.storage import ChunkStore


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
    ask.add_argument("--top-k", type=int, default=5)
    ask.add_argument("--max-chars", type=int, default=6000)

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


def _print_answer(
    query: str,
    decision: RouteDecision,
    answer_result: AnswerResult,
    context_bundle: ContextBundle,
) -> None:
    print("Question:")
    print(query)
    print(f"\nRoute: {decision.query_type.value} ({decision.decision_source.value})")
    print("\nAnswer:")
    print(answer_result.answer)
    print("\nSources:")
    citations = {
        item.citation.label: item.citation for item in context_bundle.items
    }
    if not answer_result.used_citations:
        print("(none)")
        return
    for label in answer_result.used_citations:
        print(format_source(citations[label]))


def _query_router(settings: Settings) -> QueryRouter:
    return QueryRouter(
        lambda: DeepSeekLLMClient(
            api_key=settings.deepseek_key(),
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            max_tokens=1024,
        )
    )


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
            builder = ContextBuilder(max_chars=args.max_chars)
            decision, results = _routed_search(settings, args.query, args.top_k)
            bundle = builder.build(args.query, results)
            if bundle.items:
                client = DeepSeekLLMClient(
                    api_key=settings.deepseek_key(),
                    base_url=settings.deepseek_base_url,
                    model=settings.deepseek_model,
                )
                answer_result = AnswerGenerator(client).generate(args.query, bundle)
            else:
                answer_result = AnswerResult(
                    answer=EMPTY_CONTEXT_ANSWER, used_citations=[]
                )
            _print_answer(args.query, decision, answer_result, bundle)
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
