"""Deterministic candidate selection and pre-execution policy enforcement."""
from devcontext.agentic.structural_coverage import anchor_symbols
from devcontext.tool_agent.models import MAX_CALLS_PER_STEP


class DecisionPolicyViolation(ValueError):
    pass


def candidate_tools(requirement, known, coverage, history):
    tools = set()
    if requirement.source_requirement in {'DOCUMENT', 'BOTH', 'ANY'}:
        tools.add('search_docs')
    if requirement.source_requirement == 'DOCUMENT':
        return tuple(sorted(tools))
    structural = [n for n in requirement.retrieval_needs if n.need_type in {'RELATION', 'PATH'}]
    tools.add('search_code')
    if not structural:
        tools.add('find_symbol')
        return tuple(sorted(tools))
    for need in structural:
        anchors = anchor_symbols(need, known)
        if not anchors:
            tools.add('find_symbol')
            continue
        for segment in need.segments:
            tool = relation_tool(segment.edge_type, segment.direction)
            if tool:
                tools.add(tool)
    # Searches stay available for missing bodies and recoverable/empty graphs.
    return tuple(sorted(tools))


def pending_graph_actions(need, symbols, relations):
    """Walk existing physical proofs to select the next unproved segment."""
    frontier = list(anchor_symbols(need, symbols))
    by_key = {s.symbol_key: s for s in symbols}
    actions = []
    for segment in need.segments:
        next_nodes = []
        for node in frontier:
            targets = []
            for relation in relations:
                if relation.edge_type != segment.edge_type:
                    continue
                if relation.source == node.symbol_key and segment.direction in {'OUTGOING','BOTH'}:
                    targets.append(relation.target)
                if relation.target == node.symbol_key and segment.direction in {'INCOMING','BOTH'}:
                    targets.append(relation.source)
            matched = [by_key[k] for k in targets if k in by_key]
            if not matched:
                tool = relation_tool(segment.edge_type, segment.direction)
                if tool:
                    actions.append((tool, node.symbol_key))
            next_nodes.extend(matched)
        frontier = next_nodes
        if not frontier:
            break
    return tuple(dict.fromkeys(actions))


def relation_tool(edge, direction):
    if edge == 'CALLS':
        return 'find_callers' if direction == 'INCOMING' else 'find_callees'
    if edge == 'CONSTRUCTS':
        return 'find_callees'
    if edge in {'IMPLEMENTS', 'OVERRIDES'} and direction == 'INCOMING':
        return 'find_implementations'
    return 'find_hierarchy' if edge in {'EXTENDS', 'IMPLEMENTS'} else None


class DecisionPolicyValidator:
    """在整步执行前检查规划策略，仅使用本步的需求级确认快照。

    候选外工具、跨需求 key、本步尚未确认的 key 或重复动作均提前拒绝；
    被拒绝批次不执行、不消耗实际调用预算，单独统计 policy violation。
    通过本层后仍须执行 ToolExecutor 的工具、参数、来源和 Symbol 硬校验。
    """
    def validate(self, decision, view, memory):
        from devcontext.tool_agent.planner import call_fingerprint
        if decision.cannot_progress and not decision.calls:
            attempted = {call_fingerprint(r.call) for r in memory.tool_history}
            import json
            for requirement in view.payload['requirements']:
                for action in requirement.get('pending_graph_actions', []):
                    fingerprint = (requirement['id'], action['tool'], json.dumps({'symbol_key':action['symbol_key']}, sort_keys=True, ensure_ascii=False))
                    if fingerprint not in attempted:
                        raise DecisionPolicyViolation('An unattempted structural action remains available')
            return
        if not 1 <= len(decision.calls) <= min(MAX_CALLS_PER_STEP, view.payload['remaining_calls']):
            raise DecisionPolicyViolation('Decision exceeds remaining call budget')
        requirements = {r['id']: r for r in view.payload['requirements']}
        seen = {call_fingerprint(r.call) for r in memory.tool_history}
        for call in decision.calls:
            requirement = requirements.get(call.requirement_id)
            if requirement is None or call.tool_name not in requirement['candidate_tools']:
                raise DecisionPolicyViolation('Tool is outside the requirement candidate set')
            if 'symbol_key' in call.arguments and call.arguments['symbol_key'] not in view.keys_for(call.requirement_id):
                raise DecisionPolicyViolation('Graph key is not requirement-scoped and confirmed in this step')
            fingerprint = call_fingerprint(call)
            if fingerprint in seen:
                raise DecisionPolicyViolation('Repeated decision')
            seen.add(fingerprint)
