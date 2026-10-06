from __future__ import annotations

import time

from devcontext.code_graph.intent import DIRECTIONS, classify_intent, symbol_hints
from devcontext.code_graph.models import GraphExpansion, GraphExpansionTrace
from devcontext.deadline import RequestDeadlineExceeded, remaining_seconds

MAX_ANCHORS = 3
MAX_HOPS = 2
MAX_NEIGHBORS = 5
MAX_ADDED_CHUNKS = 8
EDGE_RANK = {"CALLS": 0, "CONSTRUCTS": 1, "OVERRIDES": 2, "IMPLEMENTS": 3, "EXTENDS": 4}


class CodeGraphExpander:
    def __init__(self, store):
        self.store = store

    def expand(self, action, requirement, execution, round_index: int = 0) -> GraphExpansion:
        started = time.perf_counter()
        intent = classify_intent(action.query, requirement)
        trace = GraphExpansionTrace(action.action_id, requirement.id, round_index, intent, execution.strategy)
        additions = []
        if action.source_scope == "DOCUMENT":
            trace.skip_reason = "document_scope"
            self._record(trace, started)
            return GraphExpansion([], trace)
        code = execution.source_candidates.get("CODE")
        if code is None:
            code = [result for result in execution.results if result.source_type == "CODE"]
        hints = symbol_hints(action.query)
        if not code and not hints:
            trace.skip_reason = "no_code_anchor"
            self._record(trace, started)
            return GraphExpansion([], trace)
        try:
            remaining_seconds()  # No arbitrary 100/200ms admission cutoff.
            with self.store.session() as session:
                exact = session.find_symbols(hints, MAX_ANCHORS) if hints else []
                mapped = session.symbols_for_chunks([result.id for result in code])
                trace.stale_anchor_count = len(code) - len(mapped)
                anchors, seen_anchors = [], set()
                for symbol in exact + mapped:
                    if symbol["id"] not in seen_anchors:
                        anchors.append(symbol)
                        seen_anchors.add(symbol["id"])
                    if len(anchors) == MAX_ANCHORS:
                        break
                trace.anchors = [row["symbol_key"] for row in anchors]
                seen_chunks = {result.id for result in execution.results}
                for result in session.chunks_for_symbols(exact):
                    if result.id not in seen_chunks:
                        additions.append(result); seen_chunks.add(result.id)
                        trace.exact_added_chunks.append(result.id)
                if not anchors:
                    trace.skip_reason = "no_indexed_symbol"
                elif intent in {"NONE", "LOCATION"}:
                    trace.skip_reason = "no_relation_intent" if intent == "NONE" else "location_only"
                else:
                    outgoing, incoming = DIRECTIONS[intent]
                    # Paths retain every physical edge; OVERRIDES costs one hop.
                    frontier = [(row, index, [], row["symbol_key"]) for index, row in enumerate(anchors)]
                    visited = {row["id"] for row in anchors}
                    for hop in range(1, MAX_HOPS + 1):
                        remaining_seconds()
                        neighbors = session.neighbors([row[0]["id"] for row in frontier], outgoing, incoming, MAX_NEIGHBORS + 1)
                        candidates = []
                        for parent, rank, path, origin in frontier:
                            rows = neighbors.get(parent["id"], [])
                            trace.truncated |= len(rows) > MAX_NEIGHBORS
                            for target in rows[:MAX_NEIGHBORS]:
                                step = {"from": parent["symbol_key"], "to": target["symbol_key"], "edge_type": target["edge_type"],
                                        "direction": target["direction"], "resolution_kind": target["resolution_kind"],
                                        "source_line": target["source_line"], "source_column": target["source_column"]}
                                candidates.append((target, rank, path + [step], origin))
                        candidates.sort(key=lambda value: (EDGE_RANK[value[0]["edge_type"]], value[1], value[0]["symbol_key"]))
                        next_frontier = []
                        for target, rank, path, origin in candidates:
                            if target["id"] in visited:
                                trace.duplicate_count += 1
                                continue
                            visited.add(target["id"])
                            next_frontier.append((target, rank, path, origin))
                        projected = session.chunks_for_symbols([row[0] for row in next_frontier])
                        chunks = {result.id: result for result in projected}
                        accepted = []
                        for target, rank, path, origin in next_frontier:
                            chunk_id = target["chunk_id"]
                            if chunk_id not in seen_chunks:
                                if len(additions) >= MAX_ADDED_CHUNKS:
                                    trace.truncated = True
                                    continue
                                additions.append(chunks[chunk_id]); seen_chunks.add(chunk_id)
                                trace.graph_added_chunks.append(chunk_id)
                            trace.paths.append({"anchor": origin, "to": target["symbol_key"], "chunk_id": chunk_id, "hop": hop, "edges": path})
                            accepted.append((target, rank, path, origin))
                        frontier = accepted
                        if not frontier or len(additions) >= MAX_ADDED_CHUNKS:
                            break
        except RequestDeadlineExceeded:
            trace.skip_reason = "request_deadline"
            additions = []
            trace.exact_added_chunks.clear(); trace.graph_added_chunks.clear(); trace.paths.clear()
        except Exception as error:
            # Optional enhancement cannot discard a successful base search.
            trace.error = f"{type(error).__name__}: {error}"
            additions = []
            trace.exact_added_chunks.clear(); trace.graph_added_chunks.clear(); trace.paths.clear()
        finally:
            self._record(trace, started)
        return GraphExpansion(additions, trace)

    @staticmethod
    def _record(trace, started):
        trace.latency_ms = (time.perf_counter() - started) * 1000
        from devcontext.observability.recorder import current
        recorder = current()
        if recorder is not None:
            recorder.graph_expansions.append(trace)
