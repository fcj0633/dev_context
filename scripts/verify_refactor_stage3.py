"""Compare delivery using six frozen Writer inputs and local text producers.

No retrieval, model service, or judge is called. Full requests stay in artifacts.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import types

from devcontext.delivery import INPUT_CITATIONS
from devcontext.execution import execution_scope
from devcontext.explanation.stream_writer import SingleStreamingTeachingWriter
from devcontext.llm.client import StreamEvent
from devcontext.models import ContextBundle
from devcontext.context import ContextBuilder
from devcontext.context.views import context_item_from_ref
from devcontext.observability.diagnostics import restore_package

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "82efc2e7a033da698a935673bca16a2e1e3bdb5f"


def original_writer():
    source = subprocess.check_output(["git", "show", f"{BASELINE}:src/devcontext/explanation/stream_writer.py"], cwd=ROOT).decode()
    module = types.ModuleType("_stage3_old_writer")
    sys.modules[module.__name__] = module
    exec(compile(source,"stage2-writer.py","exec"),module.__dict__)
    return module.SingleStreamingTeachingWriter


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots',type=Path,default=ROOT/'artifacts/refactor-stage1/live-20261014')
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/refactor-stage3/offline-verification.json')
    args=parser.parse_args()
    old=original_writer();records=[]
    paths=sorted(args.snapshots.glob('S1-*.snapshot.json'))
    if len(paths)!=6: raise SystemExit('Six snapshots required; no model or retrieval fallback')
    for path in paths:
        snapshot=json.loads(path.read_text(encoding='utf-8'))
        _,package=restore_package(snapshot['retrieval'])
        attempts=[a for a in snapshot['model_attempts'] if a.get('writer')]
        payload=json.loads(attempts[-1]['messages'][-1]['content'])
        # Early stage-one samples predate final_context. Recover bodies from the
        # exact serialized Writer input, binding each header to a frozen source.
        text=payload['evidence']
        found=[]
        for ref in package.evidence_workspace.all():
            item=context_item_from_ref(ref)
            header=ContextBuilder._render_header(item)
            position=text.find(header+'\n\n')
            if position>=0: found.append((position,header,item))
        found.sort(key=lambda value:value[0])
        items=[]
        for i,(position,header,item) in enumerate(found):
            stop=found[i+1][0]-2 if i+1<len(found) else len(text)
            body=text[position+len(header)+2:stop]
            item.truncated=body!=item.content
            item.content=body
            items.append(item)
        rendered,kept,truncated=ContextBuilder(max_chars=len(text)+1).render_items(items)
        if rendered!=text: raise AssertionError(f'{path.name}: could not restore exact Writer evidence')
        bundle=ContextBundle(payload['question'],kept,rendered,len(rendered),len(text)+1,truncated)
        plan_data=payload['plan']
        sections=[types.SimpleNamespace(id=s['id'],title=s['title'],evidence_labels=s.get('evidence_labels',[])) for s in plan_data['sections']]
        plan=types.SimpleNamespace(sections=sections,evidence_labels=tuple(dict.fromkeys(e for s in sections for e in s.evidence_labels)),to_dict=lambda:plan_data)
        available={i.citation.label for i in bundle.items if i.content.strip()}
        candidate=next((e for e in sorted(available) if e not in sections[0].evidence_labels),None)
        sample={'sample':path.stem,'source_generation_status':snapshot['outcome']['generation_status'],
            'source_evidence_status':package.retrieval_state,'writer_labels':sorted(available),
            'retrieval_labels':list(package.evidence_catalog.labels()),'cases':[]}
        for case in ['unchanged','input_scope','heading']:
            body=[]
            for i,s in enumerate(sections):
                label=next((e for e in s.evidence_labels if e in available),None)
                if case=='input_scope' and i==0 and candidate: label=candidate
                title='本地替身标题差异' if case=='heading' and i==0 else s.title
                text='本地替身正文🙂'+(f' [{label}]' if label else '')+'.'
                body.append(f'<<<SECTION:{s.id}>>>\n## {title}\n{text}\n<<<END_SECTION:{s.id}>>>\n')
            text=''.join(body)
            results=[]
            for before in (True,False):
                events=[];calls=[];chunk=[0]
                class Client:
                    model='offline';last_usage={};max_tokens=1000;last_finish_reason='stop'
                    def generate_stream(self,messages):
                        calls.append([m.to_dict() for m in messages])
                        for offset in range(0,len(text),11):
                            chunk[0]=offset//11
                            yield StreamEvent('content',text=text[offset:offset+11])
                        yield StreamEvent('finish',finish_reason='stop')
                def record(event): events.append({'chunk':chunk[0],**event.to_dict()})
                writer=old(Client,permissive=True) if before else SingleStreamingTeachingWriter(Client,permissive=True,citation_policy=INPUT_CITATIONS)
                with execution_scope(on_event=record):
                    values,trace=writer.write(payload['question'],plan,bundle,types.SimpleNamespace(max_output_tokens=1000),request_started=time.perf_counter())
                results.append({'sections':[{'id':v.section_id,'markdown':v.markdown,'citations':list(v.citations)} for v in values],
                    'status':trace['completion_status'],'calls':calls,'events':events})
            baseline,current=results
            first_delta=next((e['chunk'] for e in current['events'] if e['type']=='answer.text_delta' and e['data']['delta'].strip()),None)
            first_commit=next((e['chunk'] for e in current['events'] if e['type']=='answer.section_upsert'),None)
            passed=current['status']=='complete' and first_delta is not None and first_delta<first_commit
            if case=='unchanged': passed &= baseline['sections']==current['sections']
            if case=='input_scope' and candidate:
                passed &= candidate in current['sections'][0]['citations'] and candidate not in baseline['sections'][0]['citations']
            if case=='heading': passed &= current['sections'][0]['markdown'].startswith('## '+sections[0].title+'\n')
            sample['cases'].append({'case':case,'passed':passed,'cross_section_label':candidate if case=='input_scope' else None,
                'intentional_difference':case!='unchanged','baseline':baseline,'current':current})
        records.append(sample)
    report={'baseline_commit':BASELINE,'external_calls':0,'records':records,
        'passed':all(c['passed'] for r in records for c in r['cases'])}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'snapshots':len(records),'cases':sum(len(r['cases']) for r in records),'external_calls':0}))
    return 0 if report['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
