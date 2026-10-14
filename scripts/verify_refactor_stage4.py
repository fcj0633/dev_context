"""Six frozen inputs: whole-block selection and single-call natural Writer."""
import json
from pathlib import Path
from types import SimpleNamespace

from devcontext.application.assembly import WorkflowFactory
from devcontext.config import Settings
from devcontext.context.estimator import HeuristicTokenEstimator
from devcontext.execution import execution_scope
from devcontext.explanation.budget import OutputBudget
from devcontext.llm.client import StreamEvent
from devcontext.synthesis import AnswerInput, AnswerInputBuilder
from refactor_stage4_support import ROOT, restore_writer_snapshot


def main():
    paths=sorted((ROOT/"artifacts/refactor-stage1/live-20261014").glob("S1-*.snapshot.json"))
    if len(paths)!=6: raise SystemExit("Six local snapshots required; no retrieval fallback")
    settings=Settings(_env_file=None,answer_synthesis_mode="continuous")
    records=[]
    for path in paths:
        request,package,_,common=restore_writer_snapshot(path)
        built=AnswerInputBuilder(settings.model_capabilities(),HeuristicTokenEstimator()).build(
            request,package,OutputBudget(16000,1,False,"offline"))
        assert AnswerInput.from_dict(json.loads(json.dumps(built.to_dict()))).to_dict()==built.to_dict()
        # Every source body is unchanged, even when selected in a new order.
        refs={r.evidence_id:r for r in package.evidence_workspace.all()}
        assert all(i.content==refs[i.citation.label].content and not i.truncated for i in built.bundle.items)
        counts=[];events=[]
        class Client:
            max_tokens=16000;timeout_seconds=120;last_usage={};model="offline"
            def generate_stream(self,messages):
                counts.append(messages)
                label=next(iter(sorted(built.available_labels)),None)
                yield StreamEvent("content",text="本地替身正文🙂"+(f" [{label}]" if label else ""))
                assert any(e.type=="answer.text_delta" for e in events)
                yield StreamEvent("content",text="\n\n## 机制与条件\n冻结输入的本地接口验证。")
                yield StreamEvent("finish",finish_reason="stop")
        factory=WorkflowFactory(llm_client_factory=lambda *args,**kwargs:Client())
        host=factory.build_teaching_workflow(settings,request_policy=request.policy)
        with execution_scope(on_event=events.append):result=host.run(request,package)
        assert len(counts)==1 and result.completion_status=="complete"
        assert result.context_bundle.to_dict()==built.bundle.to_dict()
        records.append({"sample":path.stem,"retrieval_state":package.retrieval_state,
            "old_writer_labels":sorted(common.available_labels),"new_writer_labels":sorted(built.available_labels),
            "restored_labels":sorted(built.available_labels-common.available_labels),
            "selection":built.selection,"answer_input":built.to_dict(),"local_writer_calls":len(counts),
            "events":[e.to_dict() for e in events]})
    output=ROOT/"artifacts/refactor-stage4/offline-verification.json"
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps({"passed":True,"external_calls":0,"records":records},ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"passed":True,"snapshots":len(records),"external_calls":0,
        "restored_labels":{r["sample"]:r["restored_labels"] for r in records}},ensure_ascii=False))


if __name__=="__main__":main()
