import os
import pytest
from test_code_graph_integration import snapshot
from devcontext.code_graph.store import CodeGraphStore

pytestmark=[pytest.mark.integration,pytest.mark.skipif(os.getenv('DEVCONTEXT_RUN_INTEGRATION')!='1',reason='requires PostgreSQL')]


def test_induced_query_never_returns_unretrieved_neighbors(snapshot):
    settings,repository,*_=snapshot
    with CodeGraphStore(settings.database_url,repository).session() as session:
        rows=session.symbols_by_keys(['M:demo.Service#f0()','M:demo.Service#f2()'])
        relations=session.induced_relations([r['id'] for r in rows],('CALLS',))
        assert len(relations)==2  # Both physical call sites retained.
        assert {r['source'] for r in relations}=={'M:demo.Service#f0()'}
        assert {r['target'] for r in relations}=={'M:demo.Service#f2()'}
        assert session.induced_relations([rows[0]['id']],('CALLS',))==[]
        assert session.induced_relations([r['id'] for r in rows],('OVERRIDES',))==[]
