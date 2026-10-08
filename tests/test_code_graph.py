from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from devcontext.code_graph.expansion import CodeGraphExpander
from devcontext.code_graph.intent import classify_intent, symbol_hints
from devcontext.code_graph.models import AnalysisContractError, CodeSymbol, SymbolEdge, validate_snapshot
from devcontext.models import Chunk, SearchResult, SearchExecution, SearchTimings
from devcontext.observability import capture
from devcontext.planning import EvidenceRequirement
from devcontext.agentic.evidence_models import SearchAction
from devcontext.deadline import request_deadline, RequestDeadlineExceeded


def result(identifier):
    return SearchResult(identifier, "CODE", "METHOD", f"Service{identifier}.java", "void run() {}", 1, 1,
                        f"Service{identifier}", "run", "void run()", None, 1.0)


def node(identifier, edge_type="CALLS"):
    return {"id": identifier, "chunk_id": identifier, "symbol_kind": "METHOD", "symbol_key": f"M:demo.Service{identifier}#run()",
            "edge_type": edge_type, "direction": "outgoing", "source_line": 1, "source_column": 1,
            "resolution_kind": "SYMBOL_SOLVER_EXACT"}


class MemoryStore:
    repository = "fixture"
    def __init__(self, links=None, exact=None, error=False):
        self.links = links or {}; self.exact = exact or []; self.error = error; self.calls = []

    @contextmanager
    def session(self):
        if self.error:
            raise RuntimeError("database unavailable")
        yield self

    def find_symbols(self, names, limit):
        return self.exact[:limit]

    def symbols_for_chunks(self, ids):
        return [node(identifier) for identifier in ids]

    def neighbors(self, ids, outgoing, incoming, limit):
        self.calls.append((ids, outgoing, incoming, limit))
        return {identifier: self.links.get(identifier, [])[:limit] for identifier in ids}

    def chunks_for_symbols(self, nodes):
        return [result(row["chunk_id"]) for row in nodes]


def expand(store, query="调用流程", scope="CODE", ids=(1,)):
    requirement = EvidenceRequirement("ER1", query, query, "CORE", "CURRENT", scope)
    action = SearchAction("SA1", "ER1", 0, query, scope, "test", "rules")
    execution = SearchExecution([result(i) for i in ids], SearchTimings(),
                                {"CODE": [result(i) for i in ids]}, strategy="vector")
    before = deepcopy(execution)
    expansion = CodeGraphExpander(store).expand(action, requirement, execution)
    assert execution == before  # Raw results AND source candidates retain retrieval semantics.
    return expansion


def test_physical_two_hops_cycles_and_per_node_neighbor_limits():
    store = MemoryStore({1: [node(i) for i in range(3, 9)], 2: [node(9)], 3: [node(1), node(10)], 9: [node(11)], 10: [node(12)]})
    expansion = expand(store, ids=(1, 2))
    assert [r.id for r in expansion.results] == [3, 4, 5, 6, 7, 9, 10, 11]
    assert 8 not in [r.id for r in expansion.results]
    assert 12 not in [r.id for r in expansion.results]
    assert expansion.trace.truncated
    assert max(path["hop"] for path in expansion.trace.paths) == 2
    assert len(store.calls) == 2
    assert expansion.trace.duplicate_count == 1


def test_override_counts_as_a_physical_hop():
    expansion = expand(MemoryStore({1: [node(2)], 2: [node(3, "OVERRIDES")], 3: [node(4)]}))
    assert [r.id for r in expansion.results] == [2, 3]
    assert expansion.trace.paths[-1]["hop"] == 2


@pytest.mark.parametrize("scope", ["CODE", "BOTH", "ANY"])
def test_graph_enhances_every_scope_with_code(scope):
    assert [r.id for r in expand(MemoryStore({1: [node(2)]}), scope=scope).results] == [2]


def test_location_only_exact_lookup_and_document_scope():
    store = MemoryStore({1: [node(3)]}, exact=[node(2)])
    expansion = expand(store, query="Service2 在哪里定义")
    assert [r.id for r in expansion.results] == [2]
    assert expansion.trace.skip_reason == "location_only"
    assert store.calls == []
    assert expand(store, scope="DOCUMENT").results == []


def test_failure_keeps_base_results_and_trace_records_reason():
    with capture() as recorder:
        expansion = expand(MemoryStore(error=True))
    assert expansion.results == []
    assert "database unavailable" in expansion.trace.error
    assert recorder.to_dict()["graph_expansions"][0]["error"] == expansion.trace.error


def test_deadline_has_no_fixed_admission_cutoff():
    with request_deadline(0.15):
        expansion = expand(MemoryStore({1: [node(2)]}))
    assert [r.id for r in expansion.results] == [2]
    with capture() as recorder, request_deadline(0.01, started=0):
        expired = expand(MemoryStore())
        assert expired.results == []
    assert recorder.graph_expansions[0].skip_reason == "request_deadline"


def test_symbol_hints_and_intent_rules():
    assert symbol_hints("M:demo.Service#run(java.lang.String) 的调用") == ["M:demo.Service#run(java.lang.String)"]
    assert symbol_hints("foo 在哪里定义") == ["foo"]
    assert symbol_hints("UserUtil.java @Transactional") == ["Transactional"]
    assert classify_intent("谁调用 Foo，在哪里", SimpleNamespace(target="", success_criteria="")) == "CALLERS"


def test_snapshot_contract_gaps_do_not_relax_references():
    chunks = [Chunk("repo", "CODE", "CLASS", "A.java", "class A {}", 1, 1),
              Chunk("repo", "CODE", "METHOD", "A.java", "void f() {}", 1, 1)]
    symbols = [CodeSymbol("repo", "T:A", "CLASS", "A", "A", None, None, 0, "A.java", 1, 1),
               CodeSymbol("repo", "M:A#f()", "METHOD", "f", "A#f", "f()", "T:A", 1, "A.java", 1, 1)]
    validate_snapshot("repo", chunks, symbols[:1], [])  # Missing symbol coverage is permitted.
    validate_snapshot("repo", chunks, symbols, [])
    for bad in [symbols + symbols[:1], [replace(symbols[0], chunk_ref=5)], [replace(symbols[0], file_path="wrong.java")],
                [replace(symbols[1], owner_symbol_key="T:Missing")]]:
        with pytest.raises(AnalysisContractError):
            validate_snapshot("repo", chunks, bad, [])
    with pytest.raises(AnalysisContractError):
        validate_snapshot("repo", chunks, symbols, [SymbolEdge("repo", "T:A", "M:A#missing()", "CALLS", 1, 1, "SYMBOL_SOLVER_EXACT")])
