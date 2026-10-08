"""Reproducible V5 production planning, paired retrieval and answer validation.

No report enables the feature by itself. Missing dependencies are recorded,
and historical or controlled samples are never substituted for live results.
"""
import argparse
from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import psycopg
from devcontext.config import Settings
from devcontext.cli import _evidence_planner, _planner_llm_factory, _planned_workflow
from devcontext.agentic.fast import FastRetrievalPlanner
from devcontext.answer_policy import resolve_policy
from devcontext.context import ContextBuilder
from devcontext.deadline import request_deadline
from devcontext.evaluation.v5_acceptance import release_decision
from devcontext.evaluation.v5_fingerprint import configuration_hash

ROOT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def production_check(settings, output):
    cases = [json.loads(l) for l in (ROOT/'benchmark/v5-production-planner.jsonl').read_text(encoding='utf-8').splitlines()]
    records = []
    for profile in ('fast', 'full'):
        scoped = settings.model_copy(update={'answer_profile': profile, 'answer_engine_enabled': True})
        for case in cases:
            print(f'production planner {profile} {case["id"]}', flush=True)
            planner = FastRetrievalPlanner(_planner_llm_factory(scoped)) if profile == 'fast' else _evidence_planner(scoped)
            record = {'profile': profile, 'case_id': case['id'], 'positive': case['positive'], 'passed': False}
            try:
                with request_deadline(60):
                    plan = planner.plan(case['question'])
                needs = [n for r in plan.requirements for n in r.retrieval_needs]
                raw = json.loads(planner.last_response or '{}')
                native_v3 = raw.get('schema_version') == 3 and all('retrieval_needs' in r for r in raw.get('requirements', []))
                match = not any(n.need_type in {'RELATION','PATH'} for n in needs)
                if case['positive']:
                    matching = [n for n in needs if n.need_type == case['need_type']]
                    if case['need_type'] == 'RELATION':
                        matching = [n for n in matching if n.relation_spec.edge_type == case['edge_type'] and n.relation_spec.direction == case['direction'] and n.spec.anchor_requirement == case['anchor_requirement']]
                    if case['need_type'] == 'PATH':
                        matching = [n for n in matching if [vars_segment(s) for s in n.segments] == case['segments'] and n.spec.anchor_requirement == case['anchor_requirement']]
                    match = bool(matching)
                    if case['need_type'] in {'CODE','DOCUMENT'}:
                        match &= not any(n.need_type in {'RELATION','PATH'} for n in needs)
                immutable = False
                try:
                    plan.schema_version = 2
                except FrozenInstanceError:
                    immutable = True
                record.update(passed=bool(native_v3 and immutable and match and plan.decision_source == 'llm'), plan=plan.to_dict(), native_v3=native_v3,
                              response=planner.last_response, rejection=getattr(planner, 'last_error', None))
            except Exception as error:
                record['error'] = type(error).__name__
            records.append(record)
            write(output, {'schema_version':3,'status':'running','records':records})
    result = {'schema_version':3,'status':'complete','records':records,'positive_count':20,'negative_count':8,
              'passed':all(r['passed'] for r in records), 'model':settings.text_model(), 'provider':settings.llm_provider,
              'configuration_sha256':configuration_hash(settings)}
    write(output,result)
    return result


def vars_segment(segment):
    return {'edge_type':segment.edge_type,'direction':segment.direction}


def answer_check(settings, output):
    questions = (
        ('locate','OrderDelayCloseProducer 类型在哪里定义，职责是什么？'),
        ('docs','设计文档中订单支付回调如何保证幂等？请以文档为依据。'),
        ('both','结合代码和设计文档解释订单支付回调的幂等保护。'),
        ('implementation','OrderService.createTicketOrder 由哪个实现方法完成？'),
        ('calls','解释 TicketOrderController.createTicketOrder → OrderService.createTicketOrder → 实现方法的两段连续调用路径，请给出各方法正文依据。'),
        ('partial','当前代码中的限流令牌桶是否已经完整实现动态配置刷新？有哪些证据缺口？'),
    )
    records=[]
    for profile in ('fast','full'):
        for kind,query in questions:
            print(f'answer {profile} {kind}',flush=True)
            scoped=settings.model_copy(update={'tool_agent_enabled':True,'symbol_graph_enabled':False,'answer_engine_enabled':True,
                                               'answer_profile':profile,'teaching_generation_mode':'single_stream' if profile=='fast' else 'v3'})
            policy=resolve_policy(query,scoped)
            scoped=scoped.model_copy(update={'answer_request_policy':policy})
            record={'profile':profile,'kind':kind,'passed':False}
            try:
                workflow=_planned_workflow(scoped,ContextBuilder(),ContextBuilder(),answer_mode='teach',request_policy=policy)
                result=workflow.run(query,12)
                trace=result.trace.to_dict()
                status=(trace.get('teaching') or {}).get('completion_status')
                state=trace.get('evidence_package_state')
                record.update(answer=result.answer_result.to_dict(),trace=trace,generation_status=status,
                              passed=status=='complete' and not result.answer_result.invalid_citations and (kind!='partial' or state=='PARTIAL'))
            except Exception as error:
                record['error']=type(error).__name__
            records.append(record)
            write(output,{'schema_version':3,'status':'running','records':records})
    report={'schema_version':3,'status':'complete','records':records,'passed':all(r['passed'] for r in records)}
    write(output,report)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/v5/live')
    parser.add_argument('--production-only',action='store_true')
    parser.add_argument('--checks',type=Path,help='Actual Python/PostgreSQL/Java check results, not assumed pass')
    args=parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError('Use a new output directory; existing live samples cannot be overwritten')
    args.output.mkdir(parents=True,exist_ok=True)
    settings=Settings()
    run_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    production=production_check(settings,args.output/'production-planners.json')
    if args.production_only:
        return 0 if production['passed'] else 1
    semantic=oracle=answers=None
    try:
        with psycopg.connect(settings.database_url,connect_timeout=3,options='-c default_transaction_read_only=on -c statement_timeout=3000') as connection:
            row=connection.execute('SELECT count(*) FROM code_symbol WHERE repository=%s',(settings.repository_name,)).fetchone()
            if not row[0]:
                raise RuntimeError('symbol_index_unavailable')
        for kind,cases,runs in (('semantic','tool-agent-v5.jsonl',3),('oracle','retrieval-workflow-v5.jsonl',1)):
            path=args.output/f'{kind}.json'
            result=subprocess.run([sys.executable,'-m','devcontext.cli','evaluate-tool-agent','--cases',str(ROOT/'benchmark'/cases),
                '--checker',kind,'--runs',str(runs),'--output',str(path)],cwd=ROOT)
            if result.returncode:
                raise RuntimeError(f'{kind}_evaluation_failed')
            report=json.loads(path.read_text(encoding='utf-8'))
            if kind=='semantic': semantic=report
            else: oracle=report
        answers=answer_check(settings,args.output/'answers.json')
    except Exception as error:
        write(args.output/'external-blocker.json',{'schema_version':3,'status':'blocked','error':type(error).__name__,
            'semantic_completed':semantic is not None,'oracle_completed':oracle is not None,'answers_completed':answers is not None})
    checks=json.loads(args.checks.read_text(encoding='utf-8')) if args.checks else {}
    decision=release_decision(semantic,oracle,production,answers,checks)
    decision['git_commit']=run_commit
    decision['datasets']={p.name:sha256(p.read_bytes()).hexdigest() for p in (ROOT/'benchmark').glob('*v5*.jsonl')}
    write(args.output/'release-decision.json',decision)
    return 0 if decision['default_enable_allowed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
