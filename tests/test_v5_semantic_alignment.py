"""Regression from live TA07: an unrelated edge cannot corroborate a claim."""
import json
import pytest
from devcontext.agentic.coverage import CoverageChecker
from devcontext.agentic.structural_coverage import CombinedCoverage
from devcontext.agentic.evidence_models import RequirementCoverage
from devcontext.context import WorkspaceCoverageView
from devcontext.planning.retrieval_need import RetrievalNeed, RelationSpec, PathSpec, PathSegment
from test_v5_structure import workspace, req, edge, A, B, C


class SelectedEvidence:
    last_client = None
    def __init__(self, ids):
        self.ids = ids
        self.calls = 0
    def check(self, requirements, view):
        self.calls += 1
        return tuple(RequirementCoverage(r.id, 'SATISFIED', self.ids, (), 'semantic claim', 'llm') for r in requirements)


def test_semantic_anchor_only_cannot_release_relation_despite_unrelated_body():
    ws = workspace()
    ws.add_relations((edge(),), 'ER1', 0, 'tool_agent')
    semantic = SelectedEvidence((1,))
    coverage = CombinedCoverage(semantic).check((req(),), WorkspaceCoverageView(ws, 0))[0]
    assert semantic.calls == 1
    assert coverage.state == 'PARTIAL'


def test_semantic_selection_with_both_endpoints_keeps_relation_satisfied():
    ws = workspace()
    ws.add_relations((edge(),), 'ER1', 0, 'tool_agent')
    coverage = CombinedCoverage(SelectedEvidence((1, 2))).check((req(),), WorkspaceCoverageView(ws, 0))[0]
    assert coverage.state == 'SATISFIED'


def test_semantic_path_selection_requires_intermediate_method_body():
    ws = workspace(count=3)
    need = RetrievalNeed('PATH', path_spec=PathSpec((PathSegment('CALLS'), PathSegment('OVERRIDES', 'INCOMING')), anchor_hint='demo.Controller#start()'))
    ws.add_relations((edge(C, A, 'CALLS', 3, 1), edge()), 'ER1', 0, 'tool_agent')
    coverage = CombinedCoverage(SelectedEvidence((2, 3))).check((req(need),), WorkspaceCoverageView(ws, 0))[0]
    assert coverage.state == 'PARTIAL'


def test_semantic_selection_can_prove_a_different_valid_physical_edge():
    ws = workspace(count=3)
    need = RetrievalNeed('RELATION', RelationSpec('CALLS', 'OUTGOING', 'NEED_METHOD', 'demo.Api#run()'))
    ws.add_relations((edge(A, B, 'CALLS', 1, 2), edge(A, C, 'CALLS', 1, 3)), 'ER1', 0, 'tool_agent')
    coverage = CombinedCoverage(SelectedEvidence((1, 3))).check((req(need),), WorkspaceCoverageView(ws, 0))[0]
    assert coverage.state == 'SATISFIED'
    assert ws._paths['ER1', 0].nodes == (A, C)


def test_production_semantic_input_contains_needs_and_requirement_owned_relations():
    messages = []
    class Client:
        def generate(self, values):
            messages.extend(values)
            return json.dumps({'statuses': [{'requirement_id':'ER1','state':'SATISFIED', 'evidence_ids':[1,2], 'missing_criteria':[], 'reason':'both endpoints'}]})
    ws = workspace()
    ws.add_relations((edge(),), 'ER1', 0, 'tool_agent')
    CombinedCoverage(CoverageChecker(Client, max_attempts=1)).check((req(),), WorkspaceCoverageView(ws, 0))
    payload = json.loads(messages[1].content)['requirements'][0]
    assert payload['retrieval_needs'][0]['need_type'] == 'RELATION'
    relation = payload['indexed_relations'][0]
    assert (relation['source'], relation['target']) == (B, A)
    assert (relation['source_chunk_id'], relation['target_chunk_id']) == (2, 1)


def test_later_searches_do_not_evict_confirmed_requirement_anchor():
    from devcontext.planning import EvidencePlan
    from devcontext.tool_agent.models import AgentMemory, SymbolObservation
    from devcontext.tool_agent.observation import build_observation
    from devcontext.tool_agent.registry import ToolRegistry
    from devcontext.tool_agent.tools import RepositoryTools
    memory = AgentMemory(coverage=(RequirementCoverage('ER1','PARTIAL',(),('missing endpoint',),'query required','rules'),))
    for i, key in enumerate([A] + [f'M:other.C{i}#run()' for i in range(12)]):
        memory.available_symbols[key] = SymbolObservation(key, 'METHOD', i+1, 'S.java')
        memory.symbol_requirements[key] = {'ER1'}
    view = build_observation('q', EvidencePlan('q',(req(),)), memory, ToolRegistry(RepositoryTools(None,None)))
    assert A in view.keys_for('ER1')
    assert len(view.keys_for('ER1')) == 8
    assert view.payload['requirements'][0]['coverage_reason'] == 'query required'


@pytest.mark.parametrize('query,expected', [('占座 本地事务','vector'), ('TicketServiceImpl.loadTicket','keyword')])
def test_code_query_routing_does_not_implicitly_use_new_workspace_symbols(query, expected):
    from devcontext.models import SearchExecution, SearchTimings
    from devcontext.tool_agent.models import ToolCall
    from devcontext.tool_agent.tools import RepositoryTools
    strategies = []
    class Policy:
        def search_scope_with_trace(self, query, scope, top_k, code_strategy=None):
            strategies.append(code_strategy)
            return SearchExecution([], SearchTimings(), strategy=code_strategy or 'semantic')
    tools = RepositoryTools(Policy(),None)
    tools.search_code(ToolCall('T1','ER1','search_code',{'query':query}),req(),workspace(),0)
    assert strategies == [expected]


def test_missing_progress_boolean_remains_rejected_and_preserves_raw_diagnostic():
    from devcontext.planning import EvidencePlan
    from devcontext.tool_agent.models import AgentMemory
    from devcontext.tool_agent.observation import build_observation
    from devcontext.tool_agent.registry import ToolRegistry
    from devcontext.tool_agent.tools import RepositoryTools
    from devcontext.tool_agent.planner import AgentPlanner, PlannerFailed
    response = json.dumps({'actions':[{'requirement_id':'ER1','tool':'find_symbol','arguments':{'name':'demo.Api.run'},'reason':'locate'}]})
    class Client:
        def generate(self, messages):
            return response
    plan = EvidencePlan('q',(req(),))
    memory = AgentMemory(coverage=(RequirementCoverage('ER1','MISSING',(),('missing',),'no evidence','rules'),))
    view = build_observation('q',plan,memory,ToolRegistry(RepositoryTools(None,None)))
    planner = AgentPlanner(Client)
    decision = planner.plan(view,plan,memory,0)
    assert decision.decision_source == 'fallback' and memory.planner_failures == 1
    assert memory.planner_diagnostics[0]['response'] == response
    with pytest.raises(PlannerFailed):
        planner.plan(view,plan,memory,1)
    assert memory.planner_failures == 2 and not memory.tool_history
