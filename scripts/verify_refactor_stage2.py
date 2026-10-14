"""Offline stage-two comparison. All model and retrieval calls use local doubles.

Requires the integration checkpoint in Git and stage-one local snapshots.
Full evidence and request messages are written only to ignored artifacts.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import types

from devcontext.agentic import RetrievalOutcome
from devcontext.application import AskRequest, AskService, WorkflowFactory
from devcontext.config import Settings
from devcontext.explanation.v3.universal import example
from devcontext.llm.client import StreamEvent
from devcontext.observability.diagnostics import replay_answer, restore_package

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "b4b1a6bbb7962c8266a0c57c5bc872dbca1d21ba"


def forbidden(*args, **kwargs):
    raise AssertionError("offline verification attempted an external dependency")


def original_module(name, path):
    source = subprocess.check_output(["git", "show", f"{BASELINE}:{path}"], cwd=ROOT).decode("utf-8")
    module = types.ModuleType(name)
    module.__package__ = "devcontext.application"
    sys.modules[name] = module
    exec(compile(source, path, "exec"), module.__dict__)
    return module, sha256(source.encode()).hexdigest()


def compare(mode, baseline, settings, package):
    records = []
    for old in (True, False):
        events = []
        class Controller:
            def retrieve(self, request, top_k):
                events.append({"kind": "retrieval", "request": request.to_dict(), "top_k": top_k})
                return RetrievalOutcome(package, ())

        class Client:
            last_finish_reason = "stop"
            last_usage = {}
            model = "offline-double"
            reasoning_effort = "low"
            max_tokens = 16000
            timeout_seconds = 120
            def __init__(self, **options):
                self.options = {k: v for k, v in options.items() if k != "deepseek_client_type"}
                self.__dict__.update(self.options)
            def generate(self, messages):
                events.append({"kind": "model", "options": self.options,
                    "messages": [{"role": m.role, "content": m.content} for m in messages]})
                if mode == "full":
                    return json.dumps(example("WHAT")["blueprint"], ensure_ascii=False)
                if mode == "legacy":
                    return "本地替身回答 [E1]。"
                return json.dumps({"direct_answer": "解释机制", "core_mental_model": "建立关系",
                    "answer_depth": "standard", "question_kind": "WHAT", "sections": [
                        {"id": "S1", "title": "回答", "teaching_goal": "解释关系", "key_points": ["机制"],
                         "evidence_labels": ["E1"], "target_tokens": 200}]}, ensure_ascii=False)
            def generate_stream(self, messages):
                events.append({"kind": "model_stream", "options": self.options,
                    "messages": [{"role": m.role, "content": m.content} for m in messages]})
                body = example("WHAT")["body"] if mode == "full" else (
                    "<<<SECTION:S1>>>\n## 回答\n本地替身解释机制 [E1]。\n<<<END_SECTION:S1>>>")
                yield StreamEvent("content", text=body)
                yield StreamEvent("finish", finish_reason="stop")

        def client_factory(config, **options):
            return Client(**options)

        request = AskRequest(package.original_query, profile=mode if mode != "legacy" else None,
                             answer_mode="legacy" if mode == "legacy" else "teach")
        if old:
            factory = baseline.WorkflowFactory({"create_llm_client": client_factory,
                "_retrieval_controller": lambda *a, **kw: Controller()})
            # Use the original resolver and service operation, with explicit local assembly.
            old_service, _ = original_module("_stage2_original_service", "src/devcontext/application/service.py")
            service = old_service.AskService(settings, workflow_factory=factory._planned_workflow)
            old_request = old_service.AskRequest(**{k: getattr(request, k) for k in request.__dataclass_fields__})
            result = service.ask(old_request, on_section=lambda s: events.append(
                {"kind": "section", "id": s.section_id, "markdown": s.markdown, "citations": list(s.citations)}))
        else:
            service = AskService(settings, factory_builder=lambda **hooks: WorkflowFactory(
                **hooks, llm_client_factory=client_factory, retrieval_engine_factory=lambda *a, **kw: Controller(),
                retrieval_service_factory=forbidden, graph_store_factory=forbidden))
            result = service.ask(request, on_section=lambda s: events.append(
                {"kind": "section", "id": s.section_id, "markdown": s.markdown, "citations": list(s.citations)}))
        records.append({"events": events, "answer": result.answer_result.to_dict(),
            "context": result.context_bundle.to_dict(), "state": result.trace.evidence_package_state,
            "generation_status": (result.trace.teaching or {}).get("completion_status")})
    return {"mode": mode, "equal": records[0] == records[1], "baseline": records[0], "current": records[1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", type=Path, default=ROOT / "artifacts/refactor-stage1/live-20261014")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/refactor-stage2/offline-verification.json")
    args = parser.parse_args()
    baseline, source_hash = original_module("_stage2_original_assembly", "src/devcontext/application/assembly.py")
    settings = Settings(_env_file=None, deepseek_api_key="offline-only", answer_profile="fast",
                        answer_reasoning_effort=None, answer_hard_timeout_seconds=None)
    snapshots = sorted(args.snapshots.glob("S1-*.snapshot.json"))
    if len(snapshots) != 6:
        raise SystemExit("Expected six local snapshots; no retrieval or model fallback will be attempted")
    restored = [restore_package(json.loads(p.read_text(encoding="utf-8"))["retrieval"])[1] for p in snapshots]
    class AnswerOnly:
        def run(self, request, evidence, on_section=None):
            return {"labels": list(evidence.evidence_catalog.labels()), "state": evidence.retrieval_state,
                    "context": evidence.context_bundle.to_dict(), "structure": evidence.evidence_workspace.structural_metadata()}
    factory = WorkflowFactory(llm_client_factory=forbidden, retrieval_service_factory=forbidden,
        graph_store_factory=forbidden, teaching_workflow_factory=lambda settings: AnswerOnly())
    replay = []
    for path, package in zip(snapshots, restored, strict=True):
        observed = replay_answer(path, factory.build_teaching_workflow(settings))
        replay.append({"sample": path.stem, "state": observed["state"], "labels": observed["labels"],
            "equal": observed["context"] == package.context_bundle.to_dict()
                and observed["structure"] == package.evidence_workspace.structural_metadata()})
    comparisons = [compare(mode, baseline, settings, restored[-1]) for mode in ("fast", "full", "legacy")]
    report = {"baseline_commit": subprocess.check_output(["git", "rev-parse", BASELINE], cwd=ROOT, text=True).strip(),
        "baseline_assembly_sha256": source_hash, "external_calls": 0, "replays": replay,
        "comparisons": comparisons, "passed": all(r["equal"] for r in replay + comparisons)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "replays": len(replay),
        "comparisons": [{"mode": c["mode"], "equal": c["equal"], "generation_status": c["current"]["generation_status"]}
                        for c in comparisons], "external_calls": 0}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
