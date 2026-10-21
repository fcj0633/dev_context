"""Explicit mutation boundaries; runtime owns scheduling and error handling."""
from dataclasses import replace

from devcontext.agentic.evidence_models import CoverageRound, RequirementCoverage, SearchAction
from devcontext.tool_agent.models import ActionStep, SymbolObservation
from devcontext.tool_agent.state import AgentRunState, StepSnapshot
from devcontext.tool_agent.structure import ingest_tool_structure


def create_run_state(query, plan, workspace, pool):
    if plan.schema_version != 3:
        raise ValueError('Agent runtime requires normalized v3')
    state = AgentRunState(query, plan, workspace, pool)
    state.task.coverage = tuple(RequirementCoverage(r.id, 'MISSING', (), (r.success_criteria,),
                               'No evidence retrieved', 'rules') for r in plan.requirements)
    return state


def apply_tool_result(state, result, requirement, step, source_policy, repository):
    classified = [source_policy.classify(r, result.call.requirement_id) for r in result.evidence_results]
    state.pool.add_many(classified)
    owned = {r.chunk_id for r in state.workspace.for_requirement(result.call.requirement_id)}
    new_ids = state.workspace.ingest(classified, step)
    ingest_tool_structure(result, requirement, state.workspace, step, repository)
    result = replace(result, new_evidence_count=len(new_ids),
        new_symbol_count=len({s.symbol_key for s in result.observation.discovered_symbols
            if s.state == 'CONFIRMED' and s.symbol_key not in state.memory.available_symbols}),
        new_ownership_count=len({r.id for r in result.evidence_results} - owned))
    for symbol in result.observation.discovered_symbols:
        if symbol.state == 'CONFIRMED':
            state.memory.available_symbols[symbol.symbol_key] = symbol
            state.memory.symbol_requirements.setdefault(symbol.symbol_key, set()).add(result.call.requirement_id)
            state.memory.candidates.pop(symbol.symbol_key, None)
    for symbol in result.observation.candidates:
        if symbol.symbol_key not in state.memory.available_symbols:
            state.memory.candidates[symbol.symbol_key] = symbol
    metadata = replace(result, evidence_results=(), returned_ids=tuple(r.id for r in result.evidence_results))
    state.history.tool_history.append(metadata)
    return metadata


def record_search_action(state, result, step, decision_source):
    call = result.call
    if call.tool_name in {'search_code', 'search_docs'}:
        error = result.error.message if not result.completed and result.error else None
        state.history.search_history.append(SearchAction(call.call_id, call.requirement_id, step,
            call.arguments.get('query', ''), 'CODE' if call.tool_name == 'search_code' else 'DOCUMENT',
            call.reason, decision_source, error))


def update_coverage(state, coverage):
    coverage = tuple(coverage)
    if tuple(c.requirement_id for c in coverage) != tuple(r.id for r in state.plan.requirements):
        raise ValueError('Coverage must contain every requirement in plan order')
    state.task.coverage = coverage


def sync_workspace_symbols(state, step):
    for requirement in state.plan.requirements:
        for symbol in state.workspace.symbols_for(requirement.id, step):
            state.memory.available_symbols[symbol.symbol_key] = SymbolObservation(symbol.symbol_key,
                symbol.symbol_kind, symbol.chunk_id, state.workspace.get(symbol.chunk_id).citation.file_path)
            state.memory.symbol_requirements.setdefault(symbol.symbol_key, set()).add(requirement.id)


def begin_step(state):
    return StepSnapshot(state.task.coverage, frozenset(state.memory.available_symbols),
                        state.workspace.progress_snapshot())


def finish_step(state, before, calls, results, step, latency_ms, diagnostics):
    after = state.workspace.progress_snapshot()
    new_evidence = tuple(r.chunk_id for r in state.workspace.all() if r.chunk_id not in before.workspace.chunk_ids)
    new_symbols = tuple(k for k in state.memory.available_symbols if k not in before.symbol_keys)
    # Only requirements in the plan contributed to the original ownership diff.
    ids = {r.id for r in state.plan.requirements}
    new_ownership = tuple(sorted((rid, cid) for rid, cid in after.chunk_ownership - before.workspace.chunk_ownership if rid in ids))
    new_relations = after.relation_ids - before.workspace.relation_ids
    new_relation_ownership = after.relation_ownership - before.workspace.relation_ownership
    ranks = {'MISSING': 0, 'UNVERIFIED': 0, 'PARTIAL': 1, 'SATISFIED': 2}
    states = {c.requirement_id: c.state for c in before.coverage}
    progress = bool(new_evidence or new_symbols or new_ownership or new_relations or new_relation_ownership or any(
        ranks[c.state] > ranks[states[c.requirement_id]] for c in state.task.coverage))
    record = ActionStep(step + 1, before.coverage, calls, tuple(results), new_evidence, new_symbols,
        new_ownership, state.task.coverage, progress, latency_ms, len(new_relations),
        len(new_relation_ownership), dict(diagnostics))
    state.history.coverage_rounds.append(CoverageRound(step, state.task.coverage))
    state.history.steps.append(record)
    return record


def record_planner_failure(state, step, diagnostic=None, violation=None, fallback=False):
    if diagnostic is not None:
        state.control.planner_diagnostics.append({'step': step + 1, **diagnostic})
    if violation is not None:
        state.control.policy_violations.append({'step': step + 1, **violation})
    if not fallback:
        state.control.planner_failures += 1


def record_policy_fallback(state):
    state.control.policy_fallbacks += 1


def use_recovery(state):
    state.control.recovery_used = True


def record_stage(state, stage):
    state.history.stages.append(stage)


def finish_run(state, reason):
    state.control.stop_reason = reason
