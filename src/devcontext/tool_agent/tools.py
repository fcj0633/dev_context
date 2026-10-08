from functools import partial
import json

import psycopg

from devcontext.agentic.evidence_models import SearchAction
from devcontext.code_graph.models import AnalysisContractError
from devcontext.deadline import RequestDeadlineExceeded
from devcontext.llm.errors import LLMRequestError
from devcontext.observability import record_retrieval_action
from devcontext.agentic.retrieval_controller import _action_trace
from devcontext.retrieval.symbols import code_strategy_for
from devcontext.tool_agent.models import (
    RelationObservation, SymbolObservation, ToolError, ToolObservation, ToolResult,
)


class RepositoryTools:
    def __init__(self, retrieval_policy, graph_store, observer=None):
        self.retrieval_policy = retrieval_policy
        self.graph_store = graph_store
        self.observer = observer
        self.find_callers = partial(self._relations, command="callers")
        self.find_callees = partial(self._relations, command="callees")
        self.find_implementations = partial(self._relations, command="implementations")
        self.find_hierarchy = partial(self._relations, command="hierarchy")

    def search_code(self, call, requirement, workspace, step):
        return self._search(call, requirement, workspace, step, "CODE")

    def search_docs(self, call, requirement, workspace, step):
        return self._search(call, requirement, workspace, step, "DOCUMENT")

    def _search(self, call, requirement, workspace, step, scope):
        strategy, symbols = code_strategy_for(call.arguments["query"], workspace, requirement.id) if scope == "CODE" else (None, ())
        top_k = 5 if requirement.priority == "CORE" else 3
        action = SearchAction(call.call_id, requirement.id, step, call.arguments["query"], scope, call.reason)
        execution = self.retrieval_policy.search_scope_with_trace(action.query, scope, top_k, code_strategy=strategy)
        execution.symbols = symbols
        record_retrieval_action(_action_trace(action, execution, step, top_k))
        if self.observer:
            self.observer.on_action_completed(action, execution)
        confirmed = ()
        error = None
        if scope == "CODE" and execution.results and self.graph_store:
            try:
                with self.graph_store.session() as session:
                    rows = session.symbols_for_chunks([r.id for r in execution.results])
                    confirmed = tuple(SymbolObservation.from_row(r) for r in rows)
            except (AnalysisContractError, RequestDeadlineExceeded, LLMRequestError):
                raise
            except (psycopg.Error, TimeoutError, ConnectionError):
                # A graph outage must not erase successful search evidence.
                error = ToolError("TOOL_UNAVAILABLE", "Search succeeded; symbol metadata is temporarily unavailable", True)
        return ToolResult(call, "PARTIAL" if error else ("SUCCESS" if execution.results else "EMPTY"),
                          tuple(execution.results), ToolObservation(discovered_symbols=confirmed,
                          file_paths=tuple(dict.fromkeys(r.file_path for r in execution.results)), strategy=execution.strategy or strategy,
                          summary=json.dumps([{'chunk_id':r.id, 'source_type':r.source_type, 'file_path':r.file_path,
                              'class_name':r.class_name, 'symbol_name':r.symbol_name, 'heading_path':r.heading_path}
                              for r in execution.results], ensure_ascii=False)), error=error)

    def find_symbol(self, call, requirement, workspace, step):
        if self.graph_store is None:
            return ToolResult(call, "ERROR", error=ToolError("TOOL_UNAVAILABLE", "Symbol index is unavailable", True))
        with self.graph_store.session() as session:
            rows = session.find_symbols([call.arguments["name"]], limit=21)
            if not rows:
                if not session.has_symbols():
                    return ToolResult(call, "ERROR", error=ToolError("TOOL_UNAVAILABLE", "Repository has no indexed symbols; use search instead", True))
                return ToolResult(call, "EMPTY", error=ToolError("SYMBOL_NOT_FOUND", "No indexed symbol matches the name"))
            # An exact full key or signature ranks ahead of simple-name matches.
            best = rows[0]["match_rank"]
            matches = [r for r in rows if r["match_rank"] == best]
            if len(matches) != 1:
                return ToolResult(call, "AMBIGUOUS", observation=ToolObservation(
                    candidates=tuple(SymbolObservation.from_row(r, "CANDIDATE") for r in matches[:20]),
                    truncated=len(rows) > 20, summary="Resolve using contextual search or a qualified name/signature"),
                    error=ToolError("AMBIGUOUS_SYMBOL", "Name matches multiple symbols; no candidate is confirmed", True))
            confirmed = tuple(SymbolObservation.from_row(r) for r in matches)
            chunks = session.chunks_for_symbols(matches)
            return ToolResult(call, "SUCCESS" if chunks else "PARTIAL", tuple(chunks),
                              ToolObservation(discovered_symbols=confirmed, file_paths=tuple(r.file_path for r in chunks)))

    def _relations(self, call, requirement, workspace, step, *, command):
        if self.graph_store is None:
            return ToolResult(call, "ERROR", error=ToolError("TOOL_UNAVAILABLE", "Graph index is unavailable", True))
        key = call.arguments["symbol_key"]
        with self.graph_store.session() as session:
            anchors = session.symbols_by_keys([key])
            if not anchors:
                return ToolResult(call, "EMPTY", error=ToolError("SYMBOL_NOT_FOUND", "Confirmed symbol is no longer indexed"))
            if len(anchors) != 1:
                raise AnalysisContractError("symbol_key must uniquely identify an indexed symbol")
            anchor = anchors[0]
            kind = anchor["symbol_kind"]
            if command == "hierarchy" and kind not in {"CLASS", "INTERFACE"}:
                return ToolResult(call, "INVALID_ARGUMENT", error=ToolError("INVALID_ARGUMENT", "Hierarchy requires a type"))
            if command == "callers" and kind != "METHOD":
                return ToolResult(call, "INVALID_ARGUMENT", error=ToolError("INVALID_ARGUMENT", "Incoming CALLS requires a method; construction uses CONSTRUCTS"))
            if command == "callees" and kind not in {"METHOD", "CONSTRUCTOR"}:
                return ToolResult(call, "INVALID_ARGUMENT", error=ToolError("INVALID_ARGUMENT", "Calls require a method or constructor"))
            if command == "implementations" and kind == "CONSTRUCTOR":
                return ToolResult(call, "INVALID_ARGUMENT", error=ToolError("INVALID_ARGUMENT", "Constructors have no overrides"))
            directions = {"callers": ((), ("CALLS",)), "callees": (("CALLS", "CONSTRUCTS"), ()),
                          "implementations": ((), ("OVERRIDES",) if kind == "METHOD" else ("IMPLEMENTS",)),
                          "hierarchy": (("EXTENDS", "IMPLEMENTS"), ("EXTENDS", "IMPLEMENTS"))}[command]
            frontier = anchors
            seen = {anchor["id"]}
            discovered = {anchor["symbol_key"]: anchor}
            relations = []
            truncated = False
            for hop in range(1, 3 if command == "hierarchy" else 2):
                grouped = session.tool_relations([n["id"] for n in frontier], *directions, limit=6)
                next_frontier = []
                for node in frontier:
                    rows = grouped[node["id"]]
                    keys = list(dict.fromkeys(r["symbol_key"] for r in rows))
                    truncated |= len(keys) > 5
                    kept = set(keys[:5])
                    for row in rows:
                        if row["symbol_key"] not in kept:
                            continue
                        source, target = (node["symbol_key"], row["symbol_key"]) if row["direction"] == "outgoing" else (row["symbol_key"], node["symbol_key"])
                        relation = RelationObservation(source, target, row["edge_type"], row["direction"], hop,
                                                       row["resolution_kind"], row["source_line"], row["source_column"])
                        if not any((r.source, r.target, r.edge_type) == (source, target, row["edge_type"]) for r in relations):
                            relations.append(relation)
                        discovered[row["symbol_key"]] = row
                        if row["id"] not in seen:
                            seen.add(row["id"])
                            next_frontier.append(row)
                frontier = next_frontier
                if not frontier:
                    break
            neighbors = [r for k, r in discovered.items() if k != key]
            # Include the anchor body too: it proves the source side of a call.
            chunks = session.chunks_for_symbols(list(discovered.values())) if relations else []
            return ToolResult(call, "PARTIAL" if truncated else ("SUCCESS" if relations else "EMPTY"), tuple(chunks),
                              ToolObservation(discovered_symbols=tuple(SymbolObservation.from_row(r) for r in neighbors),
                              graph_relations=tuple(relations), truncated=truncated,
                              file_paths=tuple(dict.fromkeys(r.file_path for r in chunks)),
                              summary="Static indexed relations only; missing edges do not prove absence of runtime calls"))
