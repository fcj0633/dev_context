"""Test-only gated stream through the real application, writers, API and store."""
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Event
import time

from devcontext.application import AskService
from devcontext.config import Settings
from devcontext.delivery import INPUT_CITATIONS
from devcontext.execution import check_cancelled
from devcontext.explanation.stream_writer import SingleStreamingTeachingWriter
from devcontext.explanation.v3.writer import TeachingWriterV3
from devcontext.llm.client import LLMMessage, StreamEvent
from devcontext.web.api import create_app
from test_web_v1 import bundle

database = TemporaryDirectory(prefix="devcontext-e2e-")
gates = {}


class FixtureWorkflow:
    def __init__(self, settings):
        self.settings = settings

    def run(self, query, top_k, on_section=None):
        if query.startswith("continuous-"):
            return self.run_continuous(query, on_section)
        if query == "fail":
            raise RuntimeError("fixture retrieval failure")
        from devcontext.execution import publish_evidence
        publish_evidence(bundle())
        gate = gates.setdefault(query, Event()) if query.startswith("gate-") else None
        sections = [SimpleNamespace(id=f"S{i}", title=f"实现解释 {i}", evidence_labels=("E1",),
                    contract={"owned_claims":[],"may_reference":[],"dependency_claims":[]}) for i in (1,2)]
        plan = SimpleNamespace(sections=sections, evidence_labels=("E1",),
                              to_dict=lambda: {"sections":[{"id":s.id,"title":s.title} for s in sections]})
        class Client:
            last_usage = {}; last_finish_reason = "stop"; max_tokens = 1000
            def generate_stream(self, messages):
                for number in (1,2):
                    for _ in range(20 if query == "slow" else 1):
                        check_cancelled();time.sleep(.1)
                    title = "临时标题" if gate and number==1 else f"实现解释 {number}"
                    yield StreamEvent("content",text=f"<<<SECTION:S{number}>>>\n## {title}\n第一段已到达🙂 [E1] [E404]。\n")
                    if gate and number==1:
                        deadline = time.monotonic()+25
                        while not gate.wait(.05):
                            check_cancelled()
                            if time.monotonic()>deadline: raise RuntimeError("test gate was not released")
                    if query=="partial" and number==2:
                        yield StreamEvent("error",text="fixture interrupted",retryable=False);return
                    yield StreamEvent("content",text=f"项目订单方法来自真实证据 [E1]。\n```java\nvoid order() {{}}\n```\n<<<END_SECTION:S{number}>>>\n")
                yield StreamEvent("finish",finish_reason="stop")
        budget = SimpleNamespace(max_output_tokens=1000)
        if self.settings.answer_profile=="full":
            pack=SimpleNamespace(sections=sections,catalog=({"label":"E1"},))
            writer=TeachingWriterV3(Client,permissive=True,citation_policy=INPUT_CITATIONS)
            result,trace=writer.write(query,None,pack,budget,request_started=time.perf_counter(),on_section=on_section,
                                     messages=[LLMMessage("user","local fixture")])
        else:
            writer=SingleStreamingTeachingWriter(Client,permissive=True,citation_policy=INPUT_CITATIONS)
            result,trace=writer.write(query,plan,bundle(),budget,request_started=time.perf_counter(),on_section=on_section)
        return SimpleNamespace(answer_result=SimpleNamespace(answer="\n\n".join(s.markdown for s in result)),
            context_bundle=bundle(),evidence_catalog=None,trace=SimpleNamespace(teaching=trace,
            agent_retrieval={},stage_usage=[],final_coverage=[],evidence_package_state="READY",stop_reason="ready",retry_count=0))

    def run_continuous(self, query, on_section):
        from devcontext.application.assembly import WorkflowFactory
        from devcontext.answer_policy import resolve_policy
        from devcontext.request import UserRequest, AnswerOptions
        gate=gates.setdefault(query,Event())
        next_gate=gates.setdefault(query+"-next",Event())
        def wait(value):
            end=time.monotonic()+25
            while not value.wait(.05):
                check_cancelled()
                if time.monotonic()>end:raise RuntimeError("test gate was not released")
        class Client:
            max_tokens=1000;timeout_seconds=120;last_usage={}
            def generate_stream(self,messages):
                yield StreamEvent("content",text="直接回答已经可见🙂 [E1]。")
                wait(gate)
                yield StreamEvent("content",text="\n\n## 机制与条件\n第二段正在生成 [E404]。\n```java\n## 代码中的标题\nvoid order() {}\n```\n")
                wait(next_gate)
                yield StreamEvent("content",text="条件来自证据 [E1]。")
                yield StreamEvent("finish",finish_reason="stop")
        factory=WorkflowFactory(llm_client_factory=lambda *a,**k:Client())
        settings=self.settings.model_copy(update={"answer_synthesis_mode":"continuous"})
        req=UserRequest(query,AnswerOptions("teach"),policy=resolve_policy(query,settings,profile=settings.answer_profile))
        package=SimpleNamespace(evidence_workspace=None,context_bundle=bundle(),retrieval_state="READY",
            evidence_plan=SimpleNamespace(requirements=()),requirement_coverage=(),unresolved_requirements=())
        result=factory.build_teaching_workflow(settings,request_policy=req.policy).run(req,package,on_section=on_section)
        return SimpleNamespace(answer_result=result.answer,context_bundle=result.context_bundle,evidence_catalog=None,
            trace=SimpleNamespace(teaching=result.stats["trace"],agent_retrieval={},stage_usage=list(result.stages),
                final_coverage=[],evidence_package_state="READY",stop_reason="ready",retry_count=0))


settings=Settings(_env_file=None)
service=AskService(settings,workflow_factory=lambda settings,*args:FixtureWorkflow(settings))
app=create_app(database_path=f"{database.name}/web.db",settings=settings,service=service)


@app.post("/fixture/release/{query}")
def release(query: str):
    gate=gates.get(query)
    if gate is None: return {"released":False}
    gate.set()
    return {"released":True}
