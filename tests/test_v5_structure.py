from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import pytest

from devcontext.agentic.structural_coverage import CombinedCoverage, StructuralCoverageChecker
from devcontext.agentic.evidence_models import RequirementCoverage, ToolExecutionSummary, package_state
from devcontext.context import EvidenceWorkspace, WorkspaceCoverageView, ContextBuilder
from devcontext.context.relations import EvidenceSymbol, EvidenceRelation, RelationProbe
from devcontext.code_graph.hydration import GraphMetadataHydrator
from devcontext.evidence import SourcePolicy
from devcontext.models import SearchResult
from devcontext.planning import EvidencePlan, EvidenceRequirement
from devcontext.planning.retrieval_need import RetrievalNeed, RelationSpec, PathSpec, PathSegment
from devcontext.tool_agent.models import AgentMemory, AgentDecision, SymbolObservation, ToolCall
from devcontext.tool_agent.observation import build_observation
from devcontext.tool_agent.policy import DecisionPolicyValidator, DecisionPolicyViolation
from devcontext.tool_agent.registry import ToolRegistry
from devcontext.tool_agent.tools import RepositoryTools

A, B, C = 'M:demo.Api#run()', 'M:demo.Impl#run()', 'M:demo.Controller#start()'


def chunk(i, kind='METHOD', source='CODE'):
    return SearchResult(i, source, kind, f'S{i}.java', 'void run() {}', 1, 2, 'S', 'run', 'run()', None, 1.)


def req(need=None, source='CODE', rid='ER1'):
    return EvidenceRequirement(rid, 'implementation', 'actual indexed implementation', 'CORE', 'CURRENT', source,
        (need or RetrievalNeed('RELATION', RelationSpec('OVERRIDES', 'INCOMING', 'NEED_METHOD', 'demo.Api#run()')),))


def workspace(rid='ER1', count=2):
    ws = EvidenceWorkspace('q')
    ws.ingest([SourcePolicy().classify(chunk(i), rid) for i in range(1, count + 1)], 0)
    ws.add_symbols([EvidenceSymbol(k, 'METHOD', i) for i, k in enumerate((A, B, C)[:count], 1)], rid, 0)
    return ws


def edge(source=B, target=A, kind='OVERRIDES', source_id=2, target_id=1):
    return EvidenceRelation('fixture', source, target, kind, 'SYMBOL_SOLVER_EXACT', 1, 1, source_id, target_id)


class Semantic:
    last_client = None
    def __init__(self, state='SATISFIED'):
        self.calls = 0
        self.state = state
    def check(self, requirements, view):
        self.calls += 1
        return tuple(RequirementCoverage(r.id, self.state, tuple(i.chunk_id for i in view.items_for(r.id)), (), 'semantic', 'rules') for r in requirements)


@pytest.mark.parametrize('status,state', [('NOT_QUERIED','UNVERIFIED'),('COMPLETED','PARTIAL'),('TIMEOUT','UNVERIFIED'),('INDEX_UNAVAILABLE','UNVERIFIED'),('FAILED','UNVERIFIED'),('DEADLINE','UNVERIFIED')])
def test_probe_states_block_semantic_without_claiming_runtime_absence(status, state):
    ws = workspace()
    ws.add_probe(RelationProbe('ER1', (0,), (A, B), ('OVERRIDES',), ('INCOMING',), 0, status))
    semantic = Semantic()
    result = CombinedCoverage(semantic).check((req(),), WorkspaceCoverageView(ws, 0))[0]
    assert result.state == state
    assert semantic.calls == 0
    assert '不存在' in result.reason  # Explicitly says it does not establish absence.


def test_source_check_precedes_structure_and_semantic():
    semantic = Semantic()
    result = CombinedCoverage(semantic).check((req(source='BOTH'),), WorkspaceCoverageView(workspace(), 0))[0]
    assert result.state == 'PARTIAL'
    assert semantic.calls == 0


def test_incoming_proof_preserves_physical_source_and_target():
    ws = workspace()
    relation = edge()
    ws.add_relations((relation,), 'ER1', 0, 'tool_agent')
    semantic = Semantic()
    before = EvidencePlan('q', (req(),)).to_dict()
    result = CombinedCoverage(semantic).check((req(),), WorkspaceCoverageView(ws, 0))[0]
    assert result.state == 'SATISFIED' and semantic.calls == 1
    path = ws._paths['ER1', 0]
    assert path.nodes == (A, B) and path.directions == ('INCOMING',)
    assert ws.relations_for('ER1')[0].source == B
    assert EvidencePlan('q', (req(),)).to_dict() == before


@pytest.mark.parametrize('semantic_state', ('PARTIAL', 'UNVERIFIED'))
def test_structure_cannot_upgrade_semantic(semantic_state):
    ws = workspace()
    ws.add_relations((edge(),), 'ER1', 0, 'auto_graph')
    assert CombinedCoverage(Semantic(semantic_state)).check((req(),), WorkspaceCoverageView(ws, 0))[0].state == semantic_state


def test_method_summary_cannot_replace_implementation_body():
    ws = workspace()
    ws._refs[2] = replace(ws._refs[2], chunk_type='CLASS')
    ws.add_relations((edge(),), 'ER1', 0, 'auto_graph')
    assert StructuralCoverageChecker().check(req(), ws, 0).state != 'SATISFIED'


def test_relation_identity_ownership_and_provenance_have_separate_progress():
    ws = workspace()
    assert ws.add_relations((edge(),), 'ER1', 0, 'auto_graph') == (1, 1)
    assert ws.add_relations((edge(),), 'ER1', 1, 'tool_agent', 'new-source') == (0, 0)
    ws.ingest([SourcePolicy().classify(chunk(i), 'ER2') for i in (1, 2)], 1)
    ws.add_symbols(ws.symbols_for('ER1'), 'ER2', 1)
    assert ws.add_relations((edge(),), 'ER2', 1, 'tool_agent') == (0, 1)
    assert not ws.relations_for('ER2', 0)
    assert not WorkspaceCoverageView(ws, 0).items_for('ER2')
    ws.freeze()
    with pytest.raises(RuntimeError):
        ws.add_probe(RelationProbe('ER1', (0,), (A,), ('CALLS',), ('OUTGOING',), 2, 'COMPLETED'))
    with pytest.raises(RuntimeError):
        ws.add_relations((edge(),), 'ER1', 2, 'auto_graph')


def test_path_requires_continuity_and_preserves_reverse_segment():
    ws = workspace(count=3)
    need = RetrievalNeed('PATH', path_spec=PathSpec((PathSegment('CALLS'), PathSegment('OVERRIDES', 'INCOMING')), anchor_hint='demo.Controller#start()'))
    ws.add_relations((edge(C, A, 'CALLS', 3, 1),), 'ER1', 0, 'tool_agent')
    assert StructuralCoverageChecker().check(req(need), ws, 0).state != 'SATISFIED'
    ws.add_relations((edge(),), 'ER1', 1, 'tool_agent')
    assert StructuralCoverageChecker().check(req(need), ws, 0).state != 'SATISFIED'
    assert StructuralCoverageChecker().check(req(need), ws, 1).state == 'SATISFIED'
    assert ws._paths['ER1', 0].nodes == (C, A, B)
    assert ws.relations_for('ER1')[1].source == B


def test_failed_unrelated_probe_does_not_revoke_proof():
    ws = workspace()
    ws.add_relations((edge(),), 'ER1', 0, 'auto_graph')
    ws.add_probe(RelationProbe('ER1', (0,), (A,), ('CALLS',), ('OUTGOING',), 0, 'TIMEOUT'))
    assert StructuralCoverageChecker().check(req(), ws, 0).state == 'SATISFIED'


def test_hydration_batches_needs_and_cannot_discover_neighbor_bodies():
    class Store:
        repository = 'fixture'
        queries = []
        @contextmanager
        def session(self):
            yield self
        def has_symbols(self):
            return True
        def symbols_for_chunks(self, ids):
            assert ids == [1, 2]
            return [{'id': i, 'symbol_key': k, 'symbol_kind':'METHOD','chunk_id':i} for i, k in enumerate((A,B),1)]
        def induced_relations(self, ids, types):
            self.queries.append((ids, types))
            assert types == ('CALLS', 'OVERRIDES')
            return []
        def neighbors(self, *args):
            pytest.fail('Fixed metadata must never expand neighbors')
    store = Store()
    ws = workspace()
    r = replace(req(), retrieval_needs=req().retrieval_needs + (RetrievalNeed('RELATION', RelationSpec('CALLS','INCOMING')),))
    hydrator = GraphMetadataHydrator(store)
    hydrator.hydrate((r,), ws, 0)
    hydrator.hydrate((r,), ws, 1)
    assert len(store.queries) == 1
    assert len(ws) == 2
    assert ws.probes_for('ER1')[0].status == 'COMPLETED'


def test_policy_rejects_other_requirement_key_and_does_not_consume_calls():
    r1, r2 = req(), req(rid='ER2')
    plan = EvidencePlan('q', (r1, r2))
    memory = AgentMemory(coverage=tuple(RequirementCoverage(r.id,'PARTIAL',(),('gap',),'test','rules') for r in plan.requirements))
    memory.available_symbols[A] = SymbolObservation(A,'METHOD',1,'A.java')
    memory.symbol_requirements[A] = {'ER1'}
    view = build_observation('q', plan, memory, ToolRegistry(RepositoryTools(None,None)))
    assert A in view.keys_for('ER1') and A not in view.keys_for('ER2')
    call = ToolCall('c','ER2','find_implementations',{'symbol_key': A})
    with pytest.raises(DecisionPolicyViolation):
        DecisionPolicyValidator().validate(AgentDecision((call,)),view,memory)
    assert not memory.tool_history


@pytest.mark.parametrize('reason', ('PLANNER_FAILED','DEADLINE','ALL_TOOLS_FAILED'))
def test_failure_before_retrieval_is_not_empty_and_body_failure_is_partial(reason):
    plan = EvidencePlan('q', (req(),))
    empty = ContextBuilder().build('q', [])
    assert package_state(plan, empty, (), tool_summary=ToolExecutionSummary(0,0,0), termination_reason=reason) == 'RETRIEVAL_FAILED'
    assert package_state(plan, empty, (), evidence_count=1, termination_reason=reason) == 'PARTIAL'


def test_no_evidence_remains_missing_without_semantic_call():
    semantic = Semantic()
    result = CombinedCoverage(semantic).check((req(),), WorkspaceCoverageView(EvidenceWorkspace(),0))[0]
    assert result.state == 'MISSING' and semantic.calls == 0
