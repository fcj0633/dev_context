from dataclasses import FrozenInstanceError, replace

import pytest

from devcontext.context import EvidenceWorkspace
from devcontext.context.relations import EvidenceSymbol, EvidenceRelation, RelationProbe
from devcontext.deadline import RequestDeadlineExceeded
from devcontext.evidence import EvidencePool, SourcePolicy
from devcontext.planning import EvidencePlan
from devcontext.tool_agent.models import AgentDecision, SymbolObservation, ToolObservation, ToolResult, ToolError
from devcontext.tool_agent.observation import build_observation
from devcontext.tool_agent.planner import AgentPlanner, RecoverablePlanningFailure
from devcontext.tool_agent.policy import DecisionPolicyValidator, DecisionPolicyViolation
from devcontext.tool_agent.state_updates import (
    create_run_state, apply_tool_result, begin_step, finish_step, update_coverage,
    sync_workspace_symbols,
)
from test_tool_agent import Checker, call, chunk, requirement, run, runtime


A, B = 'M:demo.Service#f1()', 'M:demo.Service#f2()'


def state():
    return create_run_state('question', EvidencePlan('question', (requirement(), requirement('ER2'))),
                            EvidenceWorkspace('question'), EvidencePool())


def apply(s, rid='ER1', key=A, index=1, candidates=()):
    return apply_tool_result(s, ToolResult(call(rid=rid), 'SUCCESS', (chunk(index),),
        ToolObservation(discovered_symbols=(SymbolObservation(key, 'METHOD', index, 'S.java'),), candidates=candidates)),
        next(r for r in s.plan.requirements if r.id == rid), 0, SourcePolicy(), 'repository')


def test_requests_own_their_containers_and_preserve_supplied_resources():
    first, second = state(), state()
    apply(first)
    first.control.planner_diagnostics.append({'test': True})
    first.control.policy_violations.append({'test': True})
    first.memory.candidates['candidate'] = SymbolObservation('candidate', 'METHOD', 2, 'C.java', 'CANDIDATE')
    assert not second.workspace and not second.pool.candidates_by_sub_question
    assert not second.memory.available_symbols and not second.memory.symbol_requirements and not second.memory.candidates
    assert not second.history.tool_history and not second.history.steps
    assert not second.control.planner_diagnostics and not second.control.policy_violations
    workspace, pool = EvidenceWorkspace(), EvidencePool()
    created = create_run_state('question', first.plan, workspace, pool)
    assert created.workspace is workspace and created.pool is pool and created.plan is first.plan


def test_planner_context_is_a_deeply_readonly_snapshot_without_runtime_resources():
    s = state()
    apply(s)
    context = s.planner_context()
    with pytest.raises(FrozenInstanceError):
        context.coverage = ()
    with pytest.raises(TypeError):
        context.available_symbols[A] = None
    with pytest.raises(AttributeError):
        context.symbol_requirements[A].add('ER2')
    with pytest.raises(TypeError):
        context.tool_history[0].call.arguments['query'] = 'changed'
    apply(s, rid='ER2')
    assert context.symbol_requirements[A] == frozenset({'ER1'})
    assert len(context.tool_history) == 1
    assert not hasattr(context, 'workspace') and not hasattr(context, 'control')


def test_metadata_history_reuses_result_and_preserves_errors_without_error_index():
    s = state()
    result = apply(s)
    assert result is s.history.tool_history[0]
    assert not result.evidence_results and result.returned_ids == (1,)
    assert s.workspace.get(1).content == chunk(1).content
    snapshot = begin_step(s)
    record = finish_step(s, snapshot, (result.call,), [result], 0, 0.0, {})
    assert record.results[0] is result
    assert not record.results[0].evidence_results


def test_candidates_never_become_capabilities_and_confirmations_are_requirement_scoped():
    s = state()
    candidate = SymbolObservation(B, 'METHOD', 2, 'B.java', 'CANDIDATE')
    apply(s, candidates=(candidate,))
    view = build_observation(s, runtime([], None).executor.registry)
    assert B in s.memory.candidates and B not in s.memory.available_symbols
    assert A in view.keys_for('ER1') and A not in view.keys_for('ER2')
    for key, rid in [(B, 'ER1'), (A, 'ER2')]:
        with pytest.raises(DecisionPolicyViolation):
            DecisionPolicyValidator().validate(AgentDecision((call('find_callees', key, rid=rid),)),
                                                view, s.planner_context().tool_history)


def test_observation_pruning_removes_capability_without_mutating_state(monkeypatch):
    s = state()
    for i in range(12):
        apply(s, key=f'M:demo.Service#f{i}()', index=i + 1)
    import devcontext.tool_agent.observation as observation
    class Estimator:
        def estimate(self, text):
            # The system prompt fits, but payloads exposing any known symbol do not.
            if text.startswith('{'):
                import json
                payload = json.loads(text)
                return 9000 if any(r['known_symbols'] for r in payload['requirements']) else 1
            return 1
    monkeypatch.setattr(observation, 'HeuristicTokenEstimator', Estimator)
    view = build_observation(s, runtime([], None).executor.registry)
    assert not view.confirmed_keys and not view.keys_for('ER1')
    assert len(s.memory.available_symbols) == 12
    with pytest.raises(DecisionPolicyViolation):
        DecisionPolicyValidator().validate(AgentDecision((call('find_callees', 'M:demo.Service#f0()'),)), view, ())


@pytest.mark.parametrize('change', ['chunk', 'symbol', 'ownership', 'relation', 'relation_ownership', 'coverage'])
def test_each_supported_change_counts_as_progress(change):
    s = state()
    apply(s)
    apply(s, key=B, index=2)
    relation = EvidenceRelation('repository', A, B, 'CALLS', 'SYMBOL_SOLVER_EXACT', 1, 1, 1, 2)
    if change == 'relation_ownership':
        s.workspace.add_relations([relation], 'ER1', 0, 'test')
        apply(s, rid='ER2')
        apply(s, rid='ER2', key=B, index=2)
    before = begin_step(s)
    if change == 'chunk':
        s.workspace.ingest([SourcePolicy().classify(chunk(3), 'ER1')], 1)
    elif change == 'symbol':
        s.workspace.add_symbols([EvidenceSymbol('M:other#f()', 'METHOD', 1)], 'ER1', 1)
        sync_workspace_symbols(s, 1)
    elif change == 'ownership':
        s.workspace.ingest([SourcePolicy().classify(chunk(1), 'ER2')], 1)
    elif change in {'relation', 'relation_ownership'}:
        s.workspace.add_relations([relation], 'ER2' if change == 'relation_ownership' else 'ER1', 1, 'test')
    else:
        update_coverage(s, tuple(replace(c, state='PARTIAL') for c in s.task.coverage))
    record = finish_step(s, before, (), [], 1, 0.0, {})
    assert record.progress
    assert record.coverage_before == before.coverage and record.coverage_after == s.task.coverage
    assert s.history.coverage_rounds[0].statuses == s.task.coverage


def test_repeated_evidence_relations_provenance_and_probes_do_not_count_as_progress():
    s = state()
    apply(s)
    apply(s, key=B, index=2)
    relation = EvidenceRelation('repository', A, B, 'CALLS', 'SYMBOL_SOLVER_EXACT', 1, 1, 1, 2)
    s.workspace.add_relations([relation], 'ER1', 0, 'test', 'first')
    before = begin_step(s)
    apply(s)
    s.workspace.add_relations([relation], 'ER1', 1, 'test', 'second')
    s.workspace.add_probe(RelationProbe('ER1', (), (A,), ('CALLS',), ('OUTGOING',), 1, 'COMPLETED'))
    record = finish_step(s, before, (), [], 1, 0.0, {})
    assert not record.progress
    assert s.workspace.progress_snapshot() == before.workspace


@pytest.mark.parametrize('method', ['ingest', 'symbols', 'relations', 'probe', 'path'])
def test_frozen_workspace_rejects_all_mutations(method):
    s = state()
    apply(s)
    s.workspace.freeze()
    with pytest.raises(RuntimeError, match='frozen'):
        if method == 'ingest':
            apply(s)
        elif method == 'symbols':
            s.workspace.add_symbols([], 'ER1', 1)
        elif method == 'relations':
            s.workspace.add_relations([], 'ER1', 1, 'test')
        elif method == 'probe':
            s.workspace.add_probe(None)
        else:
            s.workspace.add_path(None)


def test_coverage_deadline_keeps_prior_coverage_and_hydrated_symbols(monkeypatch):
    rt = runtime([[call()]], lambda c, *a: ToolResult(c, 'SUCCESS', (chunk(1),)))
    def check(requirements, view):
        view.workspace.add_symbols([EvidenceSymbol(A, 'METHOD', 1)], 'ER1', 0)
        raise RequestDeadlineExceeded('coverage deadline')
    monkeypatch.setattr(rt.coverage_checker, 'check', check)
    outcome, workspace = run(rt)
    assert outcome.state.task.coverage[0].state == 'MISSING'
    assert outcome.state.control.stop_reason == 'DEADLINE'
    assert A in outcome.state.memory.available_symbols
    assert len(workspace) == 1 and outcome.state.history.steps[0].progress


def test_hydrated_symbols_become_visible_next_round_and_diagnostics_keep_old_coverage(monkeypatch):
    rt = runtime([[call(value='first')], [call(value='second')]], lambda c, *a: ToolResult(c, 'SUCCESS', (chunk(1),)))
    seen, progress = [], []
    original_plan = rt.planner.plan
    def plan(view, context, step):
        seen.append(view.keys_for('ER1'))
        return original_plan(view, context, step)
    rt.planner.plan = plan
    original_check = rt.coverage_checker.check
    def check(requirements, view):
        view.workspace.add_symbols([EvidenceSymbol(A, 'METHOD', 1)], 'ER1', view.round_index)
        return original_check(requirements, view)
    monkeypatch.setattr(rt.coverage_checker, 'check', check)
    monkeypatch.setattr('devcontext.observability.diagnostics.record_progress',
                        lambda plan, ws, coverage: progress.append((len(ws), coverage[0].state)))
    outcome, _ = run(rt)
    assert seen == [frozenset(), frozenset({A})]
    assert progress == [(1, 'MISSING'), (1, 'PARTIAL')]
    assert outcome.state.control.stop_reason == 'NO_PROGRESS'


def test_planner_rejects_response_without_writing_run_state():
    s = state()
    class Client:
        def generate(self, messages):
            return 'not JSON'
    with pytest.raises(RecoverablePlanningFailure) as error:
        AgentPlanner(Client).plan(build_observation(s, runtime([], None).executor.registry), s.planner_context(), 0)
    assert error.value.response == 'not JSON'
    assert not s.control.planner_failures and not s.control.planner_diagnostics


def test_no_factory_fallback_does_not_spend_failure_budget():
    rt = runtime([], lambda c, *a: ToolResult(c, 'SUCCESS', (chunk(1),)), Checker(1))
    rt.planner = AgentPlanner()
    outcome, _ = run(rt)
    assert outcome.state.control.stop_reason == 'READY'
    assert outcome.state.control.planner_failures == 0


def test_malformed_history_arguments_are_readonly_and_keep_identical_fingerprints():
    from devcontext.tool_agent.planner import call_fingerprint
    s = state()
    malformed = replace(call(), arguments={'query': [{'unexpected': ['value']}]})
    result = ToolResult(malformed, 'INVALID_ARGUMENT', error=ToolError('INVALID_ARGUMENT', 'invalid'))
    apply_tool_result(s, result, s.plan.requirements[0], 0, SourcePolicy(), 'repository')
    context = s.planner_context()
    frozen_call = context.tool_history[0].call
    with pytest.raises(TypeError):
        frozen_call.arguments['query'][0]['unexpected'] = ()
    assert call_fingerprint(frozen_call) == call_fingerprint(malformed)
    assert s.history.errors == (result.error,)


def test_invalid_coverage_cannot_replace_previous_task_progress():
    s = state()
    before = s.task.coverage
    with pytest.raises(ValueError, match='every requirement'):
        update_coverage(s, before[:1])
    assert s.task.coverage is before


def test_step_limit_and_ready_priority_over_exhausted_call_budget():
    rt = runtime([[call(value=str(i))] for i in range(3)],
                 lambda c, *a: ToolResult(c, 'SUCCESS', (chunk(int(c.arguments['query']) + 1),)))
    outcome, _ = run(rt)
    assert outcome.state.control.stop_reason == 'MAX_STEPS'
    assert len(outcome.state.history.steps) == 3
    batches = [[call(value=f'{i}-{j}', identifier=f'TA{i+1}-{j+1}') for j in range(3 if i < 2 else 2)] for i in range(3)]
    counter = iter(range(1, 9))
    rt = runtime(batches, lambda c, *a: ToolResult(c, 'SUCCESS', (chunk(next(counter)),)), Checker(3))
    outcome, _ = run(rt)
    assert len(outcome.state.history.tool_history) == 8
    assert outcome.state.control.stop_reason == 'READY'
