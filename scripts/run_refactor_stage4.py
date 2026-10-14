"""At most twelve single-shot, fixed-evidence Writer calls. No retrieval/judge."""
import argparse
from dataclasses import replace
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import random
import subprocess
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from devcontext.application.assembly import WorkflowFactory
from devcontext.config import Settings
from devcontext.deadline import request_deadline
from devcontext.delivery import INPUT_CITATIONS
from devcontext.execution import execution_scope
from devcontext.explanation.stream_writer import SingleStreamingTeachingWriter
from devcontext.observability.diagnostics import capture_diagnostics, clean, record_answer_input
from refactor_stage4_support import ROOT, restore_writer_snapshot


def save(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    output=args.output.resolve()
    if not output.is_relative_to((ROOT/"artifacts").resolve()):raise SystemExit("output must be ignored artifacts/")
    if output.exists() and any(output.iterdir()):raise SystemExit("existing experiment; refusing overwrite or rerun")
    paths=sorted((ROOT/"artifacts/refactor-stage1/live-20261014").glob("S1-*.snapshot.json"))
    if len(paths)!=6:raise SystemExit("six snapshots required, no retrieval fallback")
    samples=[restore_writer_snapshot(p) for p in paths]  # fully validate before any API call
    output.mkdir(parents=True,exist_ok=True)
    settings=Settings()
    secrets=tuple(v.get_secret_value() for v in vars(settings).values() if hasattr(v,"get_secret_value"))+(settings.database_url,)
    config=settings.model_copy(update={"answer_engine_enabled":True,"answer_profile":"fast","answer_reasoning_effort":"low"})
    factory=WorkflowFactory()
    manifest={"timestamp":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),"status":"running",
        "baseline_commit":"7b7ffbf33f5cab5271b473263787435f059082a9",
        "head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "tracked_diff_sha256":sha256(subprocess.check_output(["git","diff","HEAD"],cwd=ROOT)).hexdigest(),
        "implementation_sha256":{str(p.relative_to(ROOT)).replace("\\","/"):sha256(p.read_bytes()).hexdigest()
            for p in [*sorted((ROOT/"src/devcontext/synthesis").glob("*.py")),Path(__file__).resolve(),
                ROOT/"scripts/refactor_stage4_support.py",ROOT/"src/devcontext/explanation/stream_writer.py"]},
        "config":{k:getattr(config,k) for k in ("llm_provider","deepseek_model","deepseek_answer_model","openai_model",
            "answer_profile","answer_reasoning_effort","llm_supported_reasoning_efforts")},
        "call_limit":12,"hard_timeout_seconds":120,"warmup":False,"judge_calls":0,"retrieval_calls":0,
        "runs":[],"snapshots":{p.name:sha256(p.read_bytes()).hexdigest() for p in paths}}
    def checkpoint():save(output/"manifest.json",clean(manifest,secrets))
    checkpoint()
    try:config.text_key()
    except Exception as exc:
        manifest.update(status="external_blocked",error=str(exc));checkpoint();print("Preflight blocked; zero requests");return 2
    stopped=False;consecutive_service_failures=0
    for index,(req,package,plan,common) in enumerate(samples):
        sample=paths[index].stem.replace(".snapshot","")
        for mode in (["planned","continuous"] if index%2==0 else ["continuous","planned"]):
            if stopped:
                manifest["runs"].append({"sample":sample,"mode":mode,"status":"not_executed","reason":"external blocked"})
                checkpoint();continue
            print(f"{sample} {mode}: starting single Writer attempt",flush=True)
            events=[];began=time.perf_counter();error=None;trace={};sections=()
            request=replace(req,policy=replace(req.policy,hard_timeout_seconds=120,reasoning_effort="low",profile="fast"))
            with capture_diagnostics(secrets=secrets) as diagnostic:
                try:
                    record_answer_input(common)
                    with request_deadline(120),execution_scope(on_event=events.append):
                        if mode=="continuous":
                            writer=factory.build_answer_synthesizer(config,max_attempts=1)
                            result=writer.synthesize(request,common)
                            sections,trace=result.sections,result.trace
                        else:
                            writer=SingleStreamingTeachingWriter(factory.profile_answer_factory(config),
                                permissive=True,citation_policy=INPUT_CITATIONS,max_attempts=1)
                            sections,trace=writer.write(common.question,plan,common.bundle,
                                SimpleNamespace(max_output_tokens=16000),request_started=began)
                    error=trace.get("error")
                except Exception as exc:
                    error=f"{type(exc).__name__}: {exc}"
                    trace={"completion_status":"failed","error":error}
                finally:
                    answer="\n\n".join(s.markdown for s in sections)
                    surrogate=SimpleNamespace(trace=SimpleNamespace(teaching=trace,evidence_package_state=package.retrieval_state),
                        answer_result=SimpleNamespace(answer=answer,used_citations=[c for s in sections for c in s.citations]),
                        context_bundle=common.bundle)
                    diagnostic.finish(surrogate,error)
                    diagnostic.save(output/f"{sample}-{mode}.snapshot.json")
            prefix=output/f"{sample}-{mode}"
            prefix.with_suffix(".answer.md").write_text(clean(answer,secrets),encoding="utf-8")
            save(prefix.with_suffix(".events.json"),clean([e.to_dict() for e in events],secrets))
            summary={"sample":sample,"mode":mode,"status":trace.get("completion_status"),
                "evidence_status":package.retrieval_state,"elapsed_ms":round((time.perf_counter()-began)*1000,2),
                "answer_chars":len(answer),"error":error,"timeline":diagnostic.timeline,
                "writer_calls":len(diagnostic.attempts),"trace":trace}
            manifest["runs"].append(clean(summary,secrets));checkpoint()
            print(f"  {summary['status']}; {summary['answer_chars']} chars; {summary['elapsed_ms']/1000:.2f}s",flush=True)
            fatal=any(term in (error or "").lower() for term in ("401","402","403","authentication","insufficient balance","not configured"))
            service_error=bool(error) and any(term in error.lower() for term in ("http ","curl","connection","timed out","timeout"))
            consecutive_service_failures=consecutive_service_failures+1 if service_error else 0
            if fatal or consecutive_service_failures>=2:
                stopped=True
    manifest["status"]="external_blocked" if stopped else "sampled"
    manifest["actual_writer_calls"]=sum(r.get("writer_calls",0) for r in manifest["runs"])
    if manifest["actual_writer_calls"]>12:raise AssertionError("call limit exceeded")
    checkpoint()
    cases=[json.loads(line) for line in (ROOT/"benchmark/refactor-stage1.jsonl").read_text(encoding="utf-8").splitlines()]
    ordering=random.Random(41)
    key={}
    for case in cases:
        modes=["planned","continuous"];ordering.shuffle(modes)
        key[case["id"]]={slot:mode for slot,mode in zip(("A","B"),modes)}
        pair={"question":case["question"],"must_explain":case["must_explain"],"must_not_claim":case["must_not_claim"]}
        for slot,mode in key[case["id"]].items():
            path=output/f"{case['id']}-{mode}.answer.md"
            pair[slot]=path.read_text(encoding="utf-8") if path.exists() else None
        save(output/f"{case['id']}.review-pair.json",pair)
    save(output/"review-key.json",key)
    return 2 if stopped else 0


if __name__=="__main__":raise SystemExit(main())
