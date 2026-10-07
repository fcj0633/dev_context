"""Convert real tool observations into shared workspace evidence and probes."""
from devcontext.context.relations import EvidenceSymbol, EvidenceRelation, RelationProbe


def ingest_tool_structure(result, requirement, workspace, round_index, repository):
    rid = requirement.id
    workspace.add_symbols((EvidenceSymbol(s.symbol_key, s.symbol_kind, s.chunk_id)
                           for s in result.observation.discovered_symbols if s.state == 'CONFIRMED'), rid, round_index)
    symbols = {s.symbol_key: s for s in workspace.symbols_for(rid, round_index)}
    relations = []
    for r in result.observation.graph_relations:
        if r.source not in symbols or r.target not in symbols:
            continue
        relations.append(EvidenceRelation(repository, r.source, r.target, r.edge_type, r.resolution_kind,
            r.source_line, r.source_column, symbols[r.source].chunk_id, symbols[r.target].chunk_id))
    added = workspace.add_relations(relations, rid, round_index, 'tool_agent', result.call.call_id)
    tools = {'find_callers': (('CALLS',), ('INCOMING',)),
             'find_callees': (('CALLS', 'CONSTRUCTS'), ('OUTGOING',)),
             'find_implementations': (('IMPLEMENTS', 'OVERRIDES'), ('INCOMING',)),
             'find_hierarchy': (('EXTENDS', 'IMPLEMENTS'), ('INCOMING', 'OUTGOING'))}
    if result.call.tool_name in tools:
        types, directions = tools[result.call.tool_name]
        status = 'COMPLETED' if result.completed else {'TOOL_TIMEOUT': 'TIMEOUT', 'TOOL_UNAVAILABLE': 'INDEX_UNAVAILABLE', 'DEADLINE_EXCEEDED': 'DEADLINE'}.get(result.error.code if result.error else '', 'FAILED')
        if result.error and result.error.code == 'SYMBOL_NOT_FOUND':
            status = 'INDEX_UNAVAILABLE'
        indices = tuple(i for i, n in enumerate(requirement.retrieval_needs) if n.segments and any(s.edge_type in types for s in n.segments))
        workspace.add_probe(RelationProbe(rid, indices, (result.call.arguments['symbol_key'],), types, directions,
            round_index, status, 'NEIGHBORS', tuple(r.identity for r in relations), result.observation.truncated,
            result.error.code if result.error else None, result.call.call_id))
    return added
