"""Frozen pre-refactor runtime observations, enabled only for recorded cases."""
from dataclasses import asdict
import json
from pathlib import Path

import pytest

BASELINE = Path(__file__).parent / 'fixtures' / 'agent-state-44cc833.json'
EXPECTED = json.loads(BASELINE.read_text(encoding='utf-8'))['cases']


def normalize(value):
    if isinstance(value, dict):
        return {k: (f'<{type(v).__name__}>' if isinstance(v, (float, int)) else v) if k in {'latency_ms', 'total_agent_ms', 'elapsed_ms'} else normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(v) for v in value]
    return value


@pytest.fixture(autouse=True)
def frozen_agent_behavior(request, monkeypatch):
    if request.node.nodeid not in EXPECTED:
        yield
        return
    from devcontext.context import ContextBuilder
    from devcontext.tool_agent import runtime as module
    from devcontext.agentic.tool_driven_retrieval_controller import ToolDrivenRetrievalController
    observed = {'runs': [], 'packages': []}
    original_run, original_view = module.AgentRuntime.run, module.build_observation
    original_retrieve = ToolDrivenRetrievalController._retrieve
    views = []

    def view(*args, **kwargs):
        result = original_view(*args, **kwargs)
        views.append(json.loads(json.dumps(result.payload, ensure_ascii=False)))
        return result

    def run(self, query, plan, workspace, pool):
        start = len(views)
        result = original_run(self, query, plan, workspace, pool)
        observed['runs'].append(normalize({
            'observations': views[start:], 'trace': result.trace(),
            'coverage_rounds': [r.to_dict() for r in result.coverage_rounds],
            'search_history': [s.to_dict() for s in result.search_history],
            'stages': [asdict(s) for s in result.stages],
            'workspace': [r.to_dict(include_content=True) for r in workspace.all()],
            'structure': workspace.structural_metadata(),
            'context': ContextBuilder().build(query, pool.select(plan.requirements, 5)[0]).to_dict(),
        }))
        return result

    def retrieve(self, *args, **kwargs):
        result = original_retrieve(self, *args, **kwargs)
        observed['packages'].append(normalize(result.package.to_dict()))
        return result

    monkeypatch.setattr(module, 'build_observation', view)
    monkeypatch.setattr(module.AgentRuntime, 'run', run)
    monkeypatch.setattr(ToolDrivenRetrievalController, '_retrieve', retrieve)
    yield
    assert observed == EXPECTED[request.node.nodeid]
