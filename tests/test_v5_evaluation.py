import json
from pathlib import Path
from devcontext.evaluation.tool_agent_runner import load_tool_cases, _score
from devcontext.evaluation.v5_acceptance import release_decision
from devcontext.agentic.evidence_models import EvidencePackage, RequirementCoverage
from devcontext.agentic.retrieval_engine import RetrievalOutcome
from devcontext.context import ContextBuilder
from devcontext.planning import EvidencePlan
from test_v5_structure import req, workspace, edge, A, B


def report(kind, coverage=1., success=1., false_ready=0):
    arm={'cases':24 if kind=='oracle' else 48,'errors':0,'core_coverage':coverage,'full_case_success':success,'false_ready':false_ready}
    return {'schema_version':3,'scoring_version':3,'status':'complete','checker_kind':kind,'corpus_sha256':'same', 'configuration_sha256':'cfg', 'top_k':12,
            'summary':{a:dict(arm) for a in ('fixed','auto_graph','tool_agent')},'quality_passed':True}


def evidence_checks():
    return ({'status':'complete','passed':True,'positive_count':20,'negative_count':8,'configuration_sha256':'cfg'},
            {'status':'complete','passed':True,'records':[{}]*12},
            {k:True for k in ('python','postgresql','java','structure','policy')})


def test_absolute_pass_cannot_enable_when_agent_is_below_auto_graph():
    semantic,oracle=report('semantic'),report('oracle')
    semantic['summary']['tool_agent']['core_coverage']=.94
    semantic['summary']['tool_agent']['full_case_success']=.90
    decision=release_decision(semantic,oracle,*evidence_checks())
    assert decision['gates']['semantic_absolute_core']
    assert decision['gates']['semantic_absolute_full']
    assert not decision['default_enable_allowed']


def test_missing_production_or_postgres_checks_block_enable():
    production,answers,checks=evidence_checks()
    checks['postgresql']=False
    assert not release_decision(report('semantic'),report('oracle'),production,answers,checks)['default_enable_allowed']
    production['passed']=False
    assert not release_decision(report('semantic'),report('oracle'),production,answers,checks)['default_enable_allowed']


def test_historical_and_different_corpus_reports_are_rejected():
    semantic,oracle=report('semantic'),report('oracle')
    semantic['scoring_version']=2
    assert not release_decision(semantic,oracle,*evidence_checks())['default_enable_allowed']
    semantic['scoring_version']=3
    oracle['corpus_sha256']='changed'
    assert not release_decision(semantic,oracle,*evidence_checks())['default_enable_allowed']


def test_all_gates_allow_a_separate_default_configuration_change():
    decision=release_decision(report('semantic'),report('oracle'),*evidence_checks())
    assert decision['default_enable_allowed'] and not decision['default_enabled']


def test_configuration_changes_require_complete_paired_re_evaluation():
    semantic, oracle = report('semantic'), report('oracle')
    oracle['configuration_sha256'] = 'different'
    decision = release_decision(semantic, oracle, *evidence_checks())
    assert not decision['gates']['same_model_configuration']
    assert not decision['default_enable_allowed']


def test_new_suites_are_v3_and_do_not_mutate_historical_cases():
    root=Path(__file__).resolve().parents[1]
    cases=load_tool_cases(root/'benchmark/tool-agent-v5.jsonl')
    oracle=load_tool_cases(root/'benchmark/retrieval-workflow-v5.jsonl')
    assert len(cases)==16 and len(oracle)==24
    assert all('retrieval_needs' in r for c in cases+oracle for r in c['requirements'])
    old=json.loads((root/'benchmark/tool-agent-v1.jsonl').read_text(encoding='utf-8').splitlines()[0])
    assert 'retrieval_needs' not in old['requirements'][0]


def test_independent_relation_gold_detects_false_ready_without_checker_self_comparison():
    ws=workspace()
    plan=EvidencePlan('q',(req(),))
    bundle=ContextBuilder().build('q',[])
    coverage=(RequirementCoverage('ER1','SATISFIED',(1,2),(),'incorrect claim','llm'),)
    case={'expected_retrieval_state':'READY','requirements':[{'id':'ER1','temporal_scope':'CURRENT','priority':'CORE',
        'expected_satisfied':True,'relevant':[{'source_type':'CODE','symbol':'run'}]}], 'relation_gold':{'ER1':[[B,'OVERRIDES',A]]}}
    outcome=RetrievalOutcome(EvidencePackage('q',plan,bundle,coverage,(),'READY',(),evidence_workspace=ws),())
    before=_score(case,outcome)
    assert before['false_ready'] and not before['full_case_success']
    ws.add_relations((edge(),),'ER1',0,'auto_graph')
    after=_score(case,outcome)
    assert not after['false_ready'] and after['full_case_success']
