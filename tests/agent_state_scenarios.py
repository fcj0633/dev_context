"""Controlled scenarios runnable against both the frozen and migrated runtime."""
from dataclasses import asdict
from unittest.mock import patch

from agent_state_baseline import normalize
from test_tool_agent import runtime, call, chunk, requirement, Checker
from devcontext.agentic.retrieval_engine import FixedEvidencePlanner
from devcontext.agentic.tool_driven_retrieval_controller import ToolDrivenRetrievalController
from devcontext.deadline import RequestDeadlineExceeded
from devcontext.planning import EvidencePlan
from devcontext.request import UserRequest, AnswerOptions
from devcontext.tool_agent.models import ToolResult, ToolError, ToolObservation, SymbolObservation
from devcontext.tool_agent.planner import AgentPlanner
import devcontext.tool_agent.runtime as runtime_module

CASES = ('legacy', 'explain', 'teach', 'model_twice', 'model_then_policy', 'policy_then_model', 'coverage_deadline', 'no_useful_tool')


def scenario(name):
    observations, messages, diagnostics = [], [], []
    def handler(c, *args):
        return ToolResult(c, 'SUCCESS', (chunk(1),), ToolObservation(discovered_symbols=(
            SymbolObservation('M:demo.Service#f1()', 'METHOD', 1, 'Service1.java'),)))
    rt = runtime([[call(value='first')], [call(value='second')]], handler, Checker(1))
    if name in {'model_twice', 'model_then_policy', 'policy_then_model'}:
        bad_policy = '{"actions":[{"requirement_id":"ER1","tool":"search_docs","arguments":{"query":"wrong source"},"reason":"invalid"}],"cannot_progress":false}'
        responses = iter(['not JSON', 'not JSON'] if name == 'model_twice' else
                         ['not JSON', bad_policy] if name == 'model_then_policy' else [bad_policy, 'not JSON'])
        class Client:
            def generate(self, value):
                messages.append([asdict(m) for m in value])
                return next(responses)
        rt.planner = AgentPlanner(Client)
        rt.coverage_checker.semantic = Checker()
    elif name == 'coverage_deadline':
        def check(*args):
            raise RequestDeadlineExceeded('fixture coverage deadline')
        rt.coverage_checker.check = check
    elif name == 'no_useful_tool':
        rt.planner = AgentPlanner(lambda: type('Client', (), {'generate': lambda *a: '{"actions":[],"cannot_progress":true}'})())
    original_view = runtime_module.build_observation
    def view(*args, **kwargs):
        result = original_view(*args, **kwargs)
        import json
        observations.append(json.loads(json.dumps(result.payload, ensure_ascii=False)))
        return result
    plan = EvidencePlan('question', (requirement(),))
    with patch.object(runtime_module, 'build_observation', view), patch(
        'devcontext.observability.diagnostics.record_progress',
        lambda plan, ws, coverage: diagnostics.append({'chunks': [r.chunk_id for r in ws.all()], 'coverage': [c.to_dict() for c in coverage]})):
        result = ToolDrivenRetrievalController(FixedEvidencePlanner(plan), rt).retrieve(
            UserRequest('question', AnswerOptions(name if name in {'legacy', 'explain', 'teach'} else 'teach')), 5)
    ws = result.package.evidence_workspace
    return normalize({'observations': observations, 'model_messages': messages, 'progress_diagnostics': diagnostics,
        'package': result.package.to_dict(), 'trace': result.agent_trace,
        'stages': [asdict(s) for s in result.stage_usage],
        'workspace': [r.to_dict(include_content=True) for r in ws.all()], 'structure': result.structural_trace,
        'frozen': ws.frozen})
