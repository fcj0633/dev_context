import os

import pytest

from test_code_graph_integration import snapshot
from devcontext.code_graph.models import SymbolEdge, CodeSymbol
from devcontext.models import Chunk
from devcontext.code_graph.store import CodeGraphStore
from devcontext.tool_agent.tools import RepositoryTools
from test_tool_agent import call, requirement
from devcontext.context import EvidenceWorkspace

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("DEVCONTEXT_RUN_INTEGRATION") != "1", reason="requires PostgreSQL")]


def test_tool_query_preserves_edge_types_without_changing_neighbors(snapshot):
    settings, repository, chunks, symbols, edges, vectors, store = snapshot
    additional = SymbolEdge(repository, "M:demo.Service#f0()", "M:demo.Service#f2()", "OVERRIDES", 2, 20, "SYMBOL_SOLVER_EXACT")
    store.replace_repository_snapshot(repository, chunks, vectors, symbols, edges + [additional], "tool-fixture")
    with CodeGraphStore(settings.database_url, repository).session() as session:
        anchors = session.symbols_by_keys(["M:demo.Service#f0()", "M:demo.Service#f1()"])
        ids = [a["id"] for a in anchors]
        baseline = session.neighbors(ids, ("CALLS", "OVERRIDES"), (), limit=5)
        relations = session.tool_relations(ids, ("CALLS", "OVERRIDES"), (), limit=5)
        assert [len(baseline[i]) for i in ids] == [5, 5]
        assert [len(relations[i]) for i in ids] == [6, 5]
        assert all(len({r["neighbor_id"] for r in relations[i]}) == 5 for i in ids)
        f2 = [r for r in relations[ids[0]] if r["symbol_key"] == "M:demo.Service#f2()"]
        assert {r["edge_type"] for r in f2} == {"CALLS", "OVERRIDES"}
        assert all(r["source_column"] == 1 for r in f2 if r["edge_type"] == "CALLS")


def test_tools_return_real_directions_and_partitioned_truncation(snapshot):
    settings, repository, *_ = snapshot
    tools = RepositoryTools(None, CodeGraphStore(settings.database_url, repository))
    result = tools.find_callees(call("find_callees", "M:demo.Service#f0()"), requirement(), EvidenceWorkspace(), 0)
    assert result.status == "PARTIAL"
    assert result.observation.truncated
    assert len(result.observation.graph_relations) == 5
    assert all(r.source == "M:demo.Service#f0()" for r in result.observation.graph_relations)
    caller = tools.find_callers(call("find_callers", "M:demo.Service#f2()"), requirement(), EvidenceWorkspace(), 0)
    assert caller.status == "SUCCESS"
    relation = caller.observation.graph_relations[0]
    assert relation.source == "M:demo.Service#f0()" and relation.target == "M:demo.Service#f2()"
    assert relation.direction == "incoming"


def test_symbol_lookup_confirms_exact_name_and_ambiguity_does_not_emit_evidence(snapshot):
    settings, repository, *_ = snapshot
    tools = RepositoryTools(None, CodeGraphStore(settings.database_url, repository))
    result = tools.find_symbol(call("find_symbol", "Service.f0"), requirement(), EvidenceWorkspace(), 0)
    assert result.status == "SUCCESS"
    assert result.observation.discovered_symbols[0].state == "CONFIRMED"


def test_actual_ambiguous_symbol_has_candidates_but_no_confirmation(snapshot):
    settings, repository, chunks, symbols, edges, vectors, store = snapshot
    extra_chunk = Chunk(repository, "CODE", "CLASS", "other/Service.java", "class Service {}", 1, 1)
    extra = CodeSymbol(repository, "T:other.Service", "CLASS", "Service", "other.Service", None, None,
                       len(chunks), "other/Service.java", 1, 1)
    store.replace_repository_snapshot(repository, chunks + [extra_chunk], vectors + [[0.01] * settings.embedding_dimensions],
                                      symbols + [extra], edges, "ambiguous-fixture")
    tools = RepositoryTools(None, CodeGraphStore(settings.database_url, repository))
    result = tools.find_symbol(call("find_symbol", "Service"), requirement(), EvidenceWorkspace(), 0)
    assert result.status == "AMBIGUOUS"
    assert not result.evidence_results and not result.observation.discovered_symbols
    assert len(result.observation.candidates) == 2
    assert all(s.state == "CANDIDATE" for s in result.observation.candidates)
    exact = tools.find_symbol(call("find_symbol", "other.Service"), requirement(), EvidenceWorkspace(), 0)
    assert exact.status == "SUCCESS" and exact.observation.discovered_symbols[0].state == "CONFIRMED"


def test_callees_separates_calls_and_constructs(snapshot):
    settings, repository, chunks, symbols, edges, vectors, store = snapshot
    extra_chunk = Chunk(repository, "CODE", "CONSTRUCTOR", "Service.java", "Service() {}", 40, 40)
    extra = CodeSymbol(repository, "C:demo.Service#<init>()", "CONSTRUCTOR", "Service", "demo.Service#<init>",
                       "<init>()", "T:demo.Service", len(chunks), "Service.java", 40, 40)
    # Use f15, with only two neighbors, so neither edge competes with the cap.
    extra_edges = [SymbolEdge(repository, "M:demo.Service#f15()", "M:demo.Service#f2()", "CALLS", 17, 1, "SYMBOL_SOLVER_EXACT"),
                   SymbolEdge(repository, "M:demo.Service#f15()", extra.symbol_key, "CONSTRUCTS", 17, 10, "SYMBOL_SOLVER_EXACT")]
    store.replace_repository_snapshot(repository, chunks + [extra_chunk], vectors + [[0.01] * settings.embedding_dimensions],
                                      symbols + [extra], edges + extra_edges, "constructor-fixture")
    tools = RepositoryTools(None, CodeGraphStore(settings.database_url, repository))
    result = tools.find_callees(call("find_callees", "M:demo.Service#f15()"), requirement(), EvidenceWorkspace(), 0)
    observation = result.observation.to_dict()
    assert len(observation["calls"]) == len(observation["constructs"]) == 1
    assert observation["constructs"][0]["target"] == extra.symbol_key
    callers = tools.find_callers(call("find_callers", extra.symbol_key), requirement(), EvidenceWorkspace(), 0)
    assert callers.status == "INVALID_ARGUMENT"


def test_missing_index_is_unavailable_rather_than_a_missing_name(snapshot):
    settings, repository, *_ = snapshot
    tools = RepositoryTools(None, CodeGraphStore(settings.database_url, repository + "-missing"))
    result = tools.find_symbol(call("find_symbol", "Service"), requirement(), EvidenceWorkspace(), 0)
    assert result.status == "ERROR" and result.error.code == "TOOL_UNAVAILABLE"
    assert result.error.retryable


def test_run_state_accumulates_real_database_symbols_relations_and_coverage(snapshot):
    from dataclasses import replace
    from test_tool_agent import runtime, Checker
    from devcontext.evidence import EvidencePool
    from devcontext.planning import EvidencePlan
    from devcontext.planning.retrieval_need import RetrievalNeed, RelationSpec
    from devcontext.tool_agent.executor import ToolExecutor
    from devcontext.tool_agent.registry import ToolRegistry

    settings, repository, *_ = snapshot
    anchor = 'M:demo.Service#f0()'
    r = replace(requirement(), retrieval_needs=(RetrievalNeed('RELATION',
        RelationSpec('CALLS', 'OUTGOING', 'NEED_METHOD', 'demo.Service#f0()')),))
    rt = runtime([[call('find_symbol', 'demo.Service#f0()')], [call('find_callees', anchor)]], None, Checker(1))
    rt.executor = ToolExecutor(ToolRegistry(RepositoryTools(None, CodeGraphStore(settings.database_url, repository))))
    workspace, pool = EvidenceWorkspace('question'), EvidencePool()
    outcome = rt.run('question', EvidencePlan('question', (r,)), workspace, pool)
    assert outcome.state.workspace is workspace and outcome.state.pool is pool
    assert outcome.state.control.stop_reason == 'READY'
    assert outcome.state.task.coverage[0].satisfied
    assert len(outcome.state.history.tool_history) == 2
    assert [t.call.tool_name for t in outcome.state.history.tool_history] == ['find_symbol', 'find_callees']
    assert len(workspace.relations_for('ER1')) == 5
    assert len(workspace.all()) == 6
    assert anchor in outcome.state.memory.available_symbols
    assert all(not t.evidence_results for t in outcome.state.history.tool_history)
