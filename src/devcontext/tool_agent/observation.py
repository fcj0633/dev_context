import json
from dataclasses import asdict

from devcontext.context.estimator import HeuristicTokenEstimator
from devcontext.tool_agent.models import AgentObservationView, MAX_STEPS, MAX_TOOL_CALLS
from devcontext.tool_agent.planner import SYSTEM_PROMPT
from devcontext.tool_agent.policy import candidate_tools


def build_observation(query, plan, memory, registry, workspace=None):
    coverage = {c.requirement_id: c for c in memory.coverage}
    symbols = list(reversed(list(memory.available_symbols.values())))
    requirements = []
    exposed = set()
    for requirement in plan.requirements:
        relevant = [s for s in symbols if requirement.id in memory.symbol_requirements.get(s.symbol_key, ())]
        selected = relevant[:8]
        exposed.update(s.symbol_key for s in selected)
        c = coverage[requirement.id]
        pending = []
        if workspace is not None:
            from devcontext.tool_agent.policy import pending_graph_actions
            for need in requirement.retrieval_needs:
                if need.need_type in {'RELATION', 'PATH'}:
                    pending.extend({'tool': tool, 'symbol_key': key} for tool, key in pending_graph_actions(need, selected, workspace.relations_for(requirement.id)))
        requirements.append({**requirement.to_dict(), "state": c.state,
                             "missing": list(c.missing_criteria),
                             "known_symbols": [asdict(s) for s in selected],
                             "candidate_tools": list(candidate_tools(requirement, selected, c, memory.tool_history)),
                             "pending_graph_actions": pending})
    recent = [{"step": s.step_number, "progress": s.progress,
               "calls": [{"tool": r.call.tool_name, "requirement_id": r.call.requirement_id,
                          "arguments": r.call.arguments, "status": r.status,
                          "evidence_ids": list(r.returned_ids),
                          "error": asdict(r.error) if r.error else None} for r in s.results]}
              for s in memory.steps[-3:]]
    payload = {"query": query, "requirements": requirements,
               "candidates": [asdict(s) for s in list(memory.candidates.values())[:8]],
               "recent_steps": recent, "available_tools": registry.specifications(),
               "remaining_steps": MAX_STEPS - len(memory.steps),
               "remaining_calls": MAX_TOOL_CALLS - len(memory.tool_history)}
    estimator = HeuristicTokenEstimator()
    budget = 8000 - estimator.estimate(SYSTEM_PROMPT)
    while estimator.estimate(json.dumps(payload, ensure_ascii=False)) > budget:
        # Lose handles before losing evidence goals or tool contracts. Every
        # removed handle is also removed from the capability set below.
        largest = max(requirements, key=lambda r: len(r["known_symbols"]))
        if largest["known_symbols"]:
            largest["known_symbols"].pop()
            keys = {s['symbol_key'] for s in largest['known_symbols']}
            largest['pending_graph_actions'] = [a for a in largest['pending_graph_actions'] if a['symbol_key'] in keys]
        elif payload["candidates"]:
            payload["candidates"].pop()
        elif payload["recent_steps"]:
            payload["recent_steps"].pop(0)
        else:
            raise ValueError("Evidence goals exceed the agent observation budget")
    exposed = {s["symbol_key"] for r in requirements for s in r["known_symbols"] if s["state"] == "CONFIRMED"}
    for r in requirements:
        keys = {s["symbol_key"] for s in r["known_symbols"]}
        r["pending_graph_actions"] = [a for a in r["pending_graph_actions"] if a["symbol_key"] in keys]
    return AgentObservationView(payload, frozenset(exposed))
