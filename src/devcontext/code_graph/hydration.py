"""Batch metadata verification confined to requirement-owned evidence."""
import psycopg
from dataclasses import replace
from devcontext.code_graph.models import AnalysisContractError
from devcontext.context.relations import EvidenceSymbol, EvidenceRelation, RelationProbe
from devcontext.deadline import RequestDeadlineExceeded


def relation_from_row(row, repository):
    return EvidenceRelation(repository, row['source'], row['target'], row['edge_type'], row['resolution_kind'],
                            row['source_line'], row['source_column'], row['source_chunk_id'], row['target_chunk_id'])


class GraphMetadataHydrator:
    def __init__(self, store, source="fixed_search"):
        self.store = store
        self.source = source

    def hydrate(self, requirements, workspace, round_index):
        for requirement in requirements:
            needs = [(i, n) for i, n in enumerate(requirement.retrieval_needs) if n.need_type in {'RELATION', 'PATH'}]
            if not needs:
                continue
            types = tuple(sorted({s.edge_type for _, n in needs for s in n.segments}))
            directions = tuple(sorted({s.direction for _, n in needs for s in n.segments}))
            chunks = tuple(r.chunk_id for r in workspace.for_requirement(requirement.id) if r.citation.source_type == 'CODE'
                           and workspace._chunk_ownership_round.get((requirement.id, r.chunk_id), r.first_seen_round) <= round_index)
            indices = tuple(i for i, _ in needs)
            keys = tuple(s.symbol_key for s in workspace.symbols_for(requirement.id, round_index))
            # Reuse completed probes only for the identical endpoint set.
            done = [p for p in workspace.probes_for(requirement.id, round_index) if p.scope == 'INDUCED' and p.status == 'COMPLETED' and p.edge_types == types]
            if done and set(keys) == set(done[-1].symbol_keys) and set(chunks) == {s.chunk_id for s in workspace.symbols_for(requirement.id, round_index)}:
                workspace.add_probe(replace(done[-1], round_index=round_index, observation_id=f'hydrate-reuse:{round_index}'))
                continue
            status, error, relations = 'NOT_QUERIED', None, []
            try:
                if not chunks:
                    error = 'no_owned_code_endpoints'
                elif self.store is None:
                    status, error = 'INDEX_UNAVAILABLE', 'graph_store_unavailable'
                else:
                    with self.store.session() as session:
                        rows = session.symbols_for_chunks(list(chunks))
                        workspace.add_symbols((EvidenceSymbol(r['symbol_key'], r['symbol_kind'], r['chunk_id']) for r in rows), requirement.id, round_index)
                        keys = tuple(r['symbol_key'] for r in rows)
                        if not rows and not session.has_symbols():
                            status, error = 'INDEX_UNAVAILABLE', 'repository_has_no_symbol_index'
                        else:
                            relations = [relation_from_row(r, self.store.repository) for r in session.induced_relations([r['id'] for r in rows], types)]
                            workspace.add_relations(relations, requirement.id, round_index, self.source, f'hydrate:{round_index}')
                            status = 'COMPLETED'
            except RequestDeadlineExceeded:
                status, error = 'DEADLINE', 'request_deadline'
            except (psycopg.errors.QueryCanceled, TimeoutError):
                status, error = 'TIMEOUT', 'graph_query_timeout'
            except (psycopg.Error, ConnectionError):
                status, error = 'FAILED', 'graph_query_unavailable'
            workspace.add_probe(RelationProbe(requirement.id, indices, keys, types, directions, round_index, status,
                relation_ids=tuple(r.identity for r in relations), error=error, observation_id=f'hydrate:{round_index}'))
