from dataclasses import FrozenInstanceError
import json
import pytest

from devcontext.planning.evidence_models import EvidencePlan, EvidenceRequirement
from devcontext.planning.evidence_planner import EvidencePlanner, fallback_evidence_plan
from devcontext.planning.retrieval_need import RetrievalNeed, RelationSpec, PathSpec, PathSegment, read_need
from devcontext.agentic.fast import FastRetrievalPlanner


CASES = (
    ('OrderService 在哪里定义', RetrievalNeed('CODE')),
    ('文档如何说明缓存设计', RetrievalNeed('DOCUMENT')),
    ('解释事务的处理流程', RetrievalNeed('CODE')),
    ('谁调用 OrderService.closeOrder', RetrievalNeed('RELATION', RelationSpec('CALLS', 'INCOMING', 'NEED_METHOD', 'OrderService.closeOrder'))),
    ('OrderService.closeOrder 调用哪些方法', RetrievalNeed('RELATION', RelationSpec('CALLS', 'OUTGOING', 'NEED_METHOD', 'OrderService.closeOrder'))),
    ('OrderService 接口由谁实现', RetrievalNeed('RELATION', RelationSpec('IMPLEMENTS', 'INCOMING', 'NEED_CLASS', 'OrderService'))),
    ('OrderService.createOrder 方法由谁实现', RetrievalNeed('RELATION', RelationSpec('OVERRIDES', 'INCOMING', 'NEED_METHOD', 'OrderService.createOrder'))),
    ('PayDO 继承谁', RetrievalNeed('RELATION', RelationSpec('EXTENDS', 'OUTGOING', 'NEED_CLASS', 'PayDO'))),
    ('closePayOrder 构造哪种异常', RetrievalNeed('RELATION', RelationSpec('CONSTRUCTS', 'OUTGOING', 'NEED_METHOD', 'closePayOrder'))),
    ('Controller 到接口及实现方法的两段调用链', RetrievalNeed('PATH', path_spec=PathSpec((PathSegment('CALLS'), PathSegment('OVERRIDES', 'INCOMING'))))),
)


@pytest.mark.parametrize('query,need', CASES)
@pytest.mark.parametrize('planner_type', (EvidencePlanner, FastRetrievalPlanner))
def test_production_parser_accepts_immutable_v3_needs(query, need, planner_type):
    requirement = {'target': query, 'success_criteria': '取得直接证据', 'priority': 'CORE', 'temporal_scope': 'CURRENT',
                   'source_requirement': 'DOCUMENT' if need.need_type == 'DOCUMENT' else 'CODE', 'retrieval_needs': [need.to_dict()]}
    payload = {'schema_version': 3, 'requirements': [requirement]}
    if planner_type is FastRetrievalPlanner:
        requirement.update(query=query, reason='直接取证')
        payload = {'primary_intent': 'HOW', 'subjects': ['OrderService'], 'requirements': [requirement]}
    class Client:
        def generate(self, messages):
            assert 'retrieval_needs' in messages[0].content
            return json.dumps(payload)
    plan = planner_type(lambda: Client()).plan(query)
    assert plan.decision_source == 'llm'
    assert plan.schema_version == 3
    assert plan.requirements[0].retrieval_needs == (need,)
    before = plan.to_dict()
    with pytest.raises(FrozenInstanceError):
        plan.requirements[0].retrieval_needs[0].need_type = 'PATH'
    assert plan.to_dict() == before


@pytest.mark.parametrize('query', ('解释购票流程', '文档中的设计流程', '说明事务原理', 'BaseDO 在哪里'))
def test_legacy_fallback_does_not_invent_relations(query):
    plan = fallback_evidence_plan(query)
    assert all(n.need_type not in {'RELATION', 'PATH'} for n in plan.requirements[0].retrieval_needs)


def test_legacy_constructor_normalizes_once_and_only_writes_v3():
    r = EvidenceRequirement('ER1', '谁调用 OrderService.closeOrder', '取得调用者', 'CORE', 'CURRENT', 'CODE')
    plan = EvidencePlan('question', (r,), schema_version=2)
    assert plan.schema_version == 3
    assert plan.requirements[0].retrieval_needs[0].relation_spec.edge_type == 'CALLS'
    assert 'CONFIRMED' not in json.dumps(plan.to_dict())
    assert not r.retrieval_needs  # Import does not mutate the supplied requirement.


@pytest.mark.parametrize('raw', (
    {'need_type': 'RELATION', 'relation_spec': {'edge_type': 'CALL_CHAIN', 'direction': 'OUTGOING'}},
    {'need_type': 'RELATION', 'path_spec': {'segments': [{'edge_type': 'CALLS'}]}},
    {'need_type': 'CODE', 'relation_spec': {'edge_type': 'CALLS', 'direction': 'OUTGOING'}},
    {'need_type': 'PATH', 'path_spec': {'segments': [{'edge_type': 'CALLS'}] * 3}},
))
def test_relation_and_path_wire_contracts_are_separate(raw):
    with pytest.raises((ValueError, TypeError)):
        read_need(raw)


def test_mutable_nested_plan_data_is_rejected():
    with pytest.raises(ValueError):
        PathSpec([PathSegment('CALLS')])
    with pytest.raises(ValueError):
        EvidenceRequirement('ER1', 'q', 'q', 'CORE', 'CURRENT', 'CODE', [RetrievalNeed('CODE')])
