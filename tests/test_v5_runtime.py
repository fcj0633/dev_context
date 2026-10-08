from dataclasses import replace
import pytest
from test_tool_agent import runtime, run, call, requirement, chunk, Checker
from devcontext.tool_agent.models import ToolResult, ToolObservation, SymbolObservation
from devcontext.tool_agent.planner import PlannerFailed
from devcontext.agentic.evidence_models import package_state
from devcontext.context import ContextBuilder
from devcontext.planning import EvidencePlan
from devcontext.planning.retrieval_need import RetrievalNeed, RelationSpec


def test_policy_failure_uses_one_fallback_and_never_increments_executor_invalid_calls():
    handled=[]
    def handler(c,*args):
        handled.append(c.tool_name)
        return ToolResult(c,'SUCCESS',(chunk(1),))
    rt=runtime([[call('search_docs','wrong-source')],[call('search_docs','wrong-again')]],handler)
    outcome,ws=run(rt)
    assert handled==['search_code']
    assert outcome.memory.stop_reason=='PLANNER_FAILED'
    assert len(outcome.memory.policy_violations)==2
    assert outcome.memory.policy_fallbacks==1
    assert len(outcome.memory.tool_history)==1
    assert all(r.status!='INVALID_ARGUMENT' for r in outcome.memory.tool_history)
    assert len(ws)==1


def test_failed_planner_before_first_tool_produces_retrieval_failed():
    rt=runtime([],lambda *a:pytest.fail('No tool should run'))
    class Planner:
        last_client=None
        last_error=None
        def plan(self,*a):
            raise PlannerFailed('failed before retrieval')
    rt.planner=Planner()
    outcome,ws=run(rt)
    assert not ws and not outcome.memory.tool_history
    assert package_state(EvidencePlan('q',(requirement(),)),ContextBuilder().build('q',[]),outcome.memory.coverage,
        tool_summary=outcome.memory.execution_summary(),termination_reason=outcome.memory.stop_reason)=='RETRIEVAL_FAILED'


def test_no_progress_does_not_count_provenance_as_new_relation():
    # The full relation/provenance distinction is tested on Workspace; here
    # repeated confirmed handles and bodies also stop after a complete batch.
    key='M:demo.Service#f1()'
    def handler(c,*a):
        return ToolResult(c,'SUCCESS',(chunk(1),),ToolObservation(discovered_symbols=(SymbolObservation(key,'METHOD',1,'S.java'),)))
    outcome,ws=run(runtime([[call(value='first')],[call(value='second')]],handler))
    assert outcome.memory.stop_reason=='NO_PROGRESS'
    assert outcome.memory.steps[-1].new_relation_count==0


def test_relation_ambiguity_does_not_authorize_same_step_graph():
    key='M:demo.Service#f1()'
    r=replace(requirement(),retrieval_needs=(RetrievalNeed('RELATION',RelationSpec('CALLS','OUTGOING','NEED_METHOD','demo.Service#f1()')),))
    executed=[]
    def handler(c,*a):
        executed.append(c.tool_name)
        assert c.tool_name=='find_symbol'
        return ToolResult(c,'EMPTY')
    outcome,_=run(runtime([[call('find_symbol','f1'),call('find_callees',key)]],handler),[r])
    assert executed==['find_symbol']
    assert outcome.memory.policy_fallbacks==1
    assert len(outcome.memory.tool_history)==1
