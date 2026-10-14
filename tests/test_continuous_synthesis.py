from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from devcontext.application import AskRequest, AskService
from devcontext.application.assembly import WorkflowFactory
from devcontext.answer_policy import resolve_policy
from devcontext.config import Settings
from devcontext.context import ContextBuilder
from devcontext.context.budget import ModelCapabilities
from devcontext.context.estimator import HeuristicTokenEstimator
from devcontext.deadline import request_deadline
from devcontext.delivery import AnswerEvent, DeliveryError, Generation
from devcontext.delivery.buffer import BufferedDelivery
from devcontext.delivery.cli import EventPrinter
from devcontext.execution import CancellationToken, RequestCancelled, execution_scope
from devcontext.explanation.budget import OutputBudget
from devcontext.llm.client import StreamEvent
from devcontext.observability.diagnostics import capture_diagnostics, record_answer_input, snapshot_package, restore_package
from devcontext.request import AnswerOptions, UserRequest
from devcontext.synthesis import AnswerInput, AnswerInputBuilder, ContinuousAnswerSynthesizer
from devcontext.synthesis.markdown import MarkdownParts, clean_citations
from devcontext.synthesis.prompts import writer_messages
from devcontext.web.store import RunStore
from test_teaching_workflow import make_package

CAPS = ModelCapabilities(131072, 32768)
ESTIMATOR = HeuristicTokenEstimator()


def request(package, profile="fast", max_chars=None):
    return UserRequest(package.original_query, AnswerOptions("teach"), max_chars,
        resolve_policy(package.original_query, Settings(_env_file=None), profile=profile))


def inputs(package=None, **kwargs):
    package = package or make_package()
    req = request(package, **kwargs)
    return req, AnswerInputBuilder(CAPS, ESTIMATOR).build(req, package, OutputBudget(16000, 1, False, "test"))


class Client:
    max_tokens=16000; timeout_seconds=120; last_usage={}; last_finish_reason="stop"
    def __init__(self, events):
        self.events, self.calls = events, []
    def generate_stream(self, messages):
        self.calls.append(messages)
        yield from self.events


def test_answer_input_roundtrip_is_stable_and_diagnostic_optional():
    req, value = inputs()
    assert AnswerInput.from_dict(json.loads(json.dumps(value.to_dict()))).to_dict() == value.to_dict()
    raw = snapshot_package(req, make_package())
    raw["workspace"]["refs"].reverse()
    restored_request, restored=restore_package(raw)
    restored_input=AnswerInputBuilder(CAPS,ESTIMATOR).build(restored_request,restored,OutputBudget(16000,1,False,"test"))
    assert restored_input.to_dict()==value.to_dict()
    with capture_diagnostics(secrets=("body 1",)) as capture:
        record_answer_input(value)
    assert "body 1" not in json.dumps(capture.to_dict())
    assert "answer_input" in capture.to_dict()
    class Explosive:
        def to_dict(self): raise AssertionError("disabled capture")
    record_answer_input(Explosive())


def test_builder_skips_oversized_block_and_keeps_later_complete_body():
    package=make_package()
    items=deepcopy(package.context_bundle.items)
    items[0].content="huge "*3000
    package=replace(package,evidence_workspace=None,context_bundle=replace(package.context_bundle,items=items),retrieval_state="PARTIAL")
    _,value=inputs(package,max_chars=700)
    assert value.available_labels=={"E2"}
    assert value.bundle.items[0].content=="body 2"
    assert value.retrieval_state=="PARTIAL"
    assert value.selection[0]["reason"]=="max_chars"
    assert "ER1" in value.budget["omitted_core_requirements"]
    assert value.bundle.truncated


def test_builder_preserves_prior_truncation_empty_exclusion_and_no_renumber():
    package=make_package();items=deepcopy(package.context_bundle.items)
    items[0].content="";items[1].truncated=True
    package=replace(package,evidence_workspace=None,context_bundle=replace(package.context_bundle,items=items))
    _,value=inputs(package)
    assert value.available_labels=={"E2"} and value.bundle.items[0].truncated
    assert any(s["reason"]=="empty" for s in value.selection)
    with pytest.raises(ValueError,match="duplicate"):
        data=value.to_dict();data["bundle"]["items"]*=2;AnswerInput.from_dict(data)


def test_actual_writer_prompt_budget_includes_metadata():
    req,value=inputs()
    assert value.budget["prompt_estimated_tokens"]==sum(ESTIMATOR.estimate(m.content) for m in writer_messages(value))
    small=ModelCapabilities(1500,1000)
    with pytest.raises(ValueError,match="metadata"):
        AnswerInputBuilder(small,ESTIMATOR).build(req,make_package(),OutputBudget(1000,1,False,"small"))


@pytest.mark.parametrize("cut",range(1,35))
def test_arbitrary_markdown_splits_release_body_and_ignore_code_headings(cut):
    text="直接回答🙂 [E1]。\n\n## 自然标题\n解释。\n```java\n## 代码标题\n[E99]\n```\n\n## 下一节\n条件 [E2]。"
    events=[]
    with execution_scope(on_event=events.append):
        parts=MarkdownParts(Generation("test"),{"E1","E2"})
        for i in range(0,len(text),cut):parts.feed(text[i:i+cut])
        assert any(e.type=="answer.text_delta" for e in events)
        parts.finish()
    assert len(parts.sections)==3
    assert "## 代码标题" in parts.sections[1].markdown
    assert "[E99]" in parts.sections[1].markdown
    assert parts.sections[0].citations==("E1",)
    assert parts.sections[2].citations==("E2",)


def test_plain_text_is_published_before_newline_or_end_and_part_limit():
    events=[]
    with execution_scope(on_event=events.append):
        parts=MarkdownParts(Generation("test"),(),limit=5)
        parts.feed("中文🙂")
        assert events[-1].data["delta"]=="中文🙂"
        with pytest.raises(ValueError,match="limit"):parts.feed("too long")


def test_citations_ignore_inline_and_fenced_code():
    value="文本 [E1] [E9] [E1,E2]。`[E9]`\n```java\n[E404]\n```\n后文 [E2]"
    body,citations,warnings=clean_citations(value,{"E1","E2"})
    assert citations==("E1","E2") and len(warnings)==2
    assert "`[E9]`" in body and "[E404]" in body
    assert "[E1,E2]" not in body


def test_citation_cleanup_respects_long_fences_and_multiline_inline_code():
    value="外部 [E1]\n````java\n```\n[E999]\n````\n``inline\n[E888]`` 后文 [E9]"
    body,labels,warnings=clean_citations(value,{"E1"})
    assert "[E999]" in body and "[E888]" in body and "[E9]" not in body
    assert labels==("E1",) and len(warnings)==1


def test_answer_input_loader_rejects_changed_rendering():
    _,value=inputs()
    raw=value.to_dict();raw["bundle"]["rendered_text"]+="\n[E99] injected"
    with pytest.raises(ValueError,match="differs"):
        AnswerInput.from_dict(raw)


def test_failure_snapshot_retains_generated_unfinished_text_without_subscriber():
    req,value=inputs()
    client=Client([StreamEvent("content",text="未完成的实际正文🙂"),StreamEvent("error",text="network",retryable=True)])
    with capture_diagnostics() as capture:
        result=ContinuousAnswerSynthesizer(lambda:client).synthesize(req,value)
    raw=capture.to_dict()
    assert any(e["type"]=="answer.text_delta" and "实际正文" in e["data"]["delta"] for e in raw["answer_events"])
    assert result.trace["unfinished_text"]=="未完成的实际正文🙂"
    assert len(client.calls)==1


def test_diagnostic_event_snapshot_scrubs_credentials_split_across_deltas():
    from devcontext.execution import publish_answer
    with capture_diagnostics(secrets=("secret-key",)) as capture:
        publish_answer(AnswerEvent("answer.text_delta","g",{"section_id":"S1","delta":"正文 secret-"}))
        publish_answer(AnswerEvent("answer.text_delta","g",{"section_id":"S1","delta":"key 结束"}))
    raw=capture.to_dict()
    body="".join(e["data"].get("delta","") for e in raw["answer_events"])
    assert "secret-key" not in body and "secret-" not in body and "结束" in body


def test_section_callback_failure_escapes_after_generation_without_retry():
    req,value=inputs()
    client=Client([StreamEvent("content",text="正文 [E1]"),StreamEvent("finish",finish_reason="stop")])
    def callback(section):raise RuntimeError("storage failure")
    with pytest.raises(DeliveryError):
        ContinuousAnswerSynthesizer(lambda:client).synthesize(req,value,on_section=callback)
    assert len(client.calls)==1


def test_continuous_requests_isolate_configuration_cancellation_and_diagnostics():
    from test_teaching_workflow import FakeController
    from devcontext.execution import check_cancelled
    barrier=Barrier(2);seen={};tokens={mode:CancellationToken() for mode in ("fast","full")}
    class ConcurrentClient(Client):
        def __init__(self,profile):super().__init__([]);self.profile=profile
        def generate_stream(self,messages):
            seen[self.profile]=self.max_tokens
            barrier.wait(timeout=3)
            yield StreamEvent("content",text="独立正文 [E1]")
            yield StreamEvent("finish",finish_reason="stop")
    def build(**hooks):
        return WorkflowFactory(**hooks,llm_client_factory=lambda settings,**kw:ConcurrentClient(settings.answer_profile),
            retrieval_engine_factory=lambda settings,**kw:FakeController(make_package()))
    service=AskService(Settings(_env_file=None,answer_synthesis_mode="continuous"),factory_builder=build)
    def run_mode(profile):
        delivered=[]
        def event(value):
            delivered.append(value)
            if profile=="fast" and value.type=="answer.text_delta":tokens[profile].cancel()
        with capture_diagnostics() as diagnostic:
            try:
                result=service.ask(AskRequest("q",profile=profile),on_event=event,cancellation=tokens[profile])
                state=result.trace.teaching["completion_status"]
            except RequestCancelled:state="cancelled"
        return state,delivered,diagnostic.to_dict()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures={mode:pool.submit(run_mode,mode) for mode in tokens}
        values={mode:future.result() for mode,future in futures.items()}
    assert values["fast"][0]=="cancelled" and values["full"][0]=="complete"
    assert seen=={"fast":16000,"full":32768}
    assert {e.generation_id for e in values["fast"][1]}.isdisjoint(e.generation_id for e in values["full"][1])
    assert all(e["generation_id"] in {v.generation_id for v in values["full"][1]}
        for e in values["full"][2]["answer_events"])
    assert service.settings.answer_profile=="fast"
    check_cancelled()


def test_milestones_use_same_request_start_as_parent_workflow():
    import time
    req,value=inputs()
    client=Client([StreamEvent("content",text="正文"),StreamEvent("finish",finish_reason="stop")])
    result=ContinuousAnswerSynthesizer(lambda:client).synthesize(req,value,request_started=time.perf_counter()-10)
    assert result.trace["first_content_token_ms"]>=10000
    assert result.trace["first_section_ready_ms"]>=10000


@pytest.mark.parametrize("profile",["fast","full"])
def test_public_factory_continuous_requires_only_one_writer_call(profile):
    req,value=inputs(profile=profile)
    client=Client([StreamEvent("content",text="回答 [E1]。\n\n## 原因\n解释 [E2]。"),StreamEvent("finish",finish_reason="stop")])
    settings=Settings(_env_file=None,answer_synthesis_mode="continuous",answer_profile=profile,
                      answer_request_policy=req.policy)
    factory=WorkflowFactory(llm_client_factory=lambda *args,**kwargs:client)
    host=factory.build_teaching_workflow(settings,request_policy=req.policy)
    assert not hasattr(host,"micro_planner") and not hasattr(host,"v3_planner")
    result=host.run(req,make_package())
    assert len(client.calls)==1 and result.completion_status=="complete"
    assert result.stats["trace"]["synthesis_mode"]=="continuous"
    assert "<<<SECTION" not in client.calls[0][-1].content


def test_explicit_compatibility_mode_still_selects_planned():
    settings=Settings(_env_file=None,answer_synthesis_mode="continuous")
    service=AskService(settings)
    assert service.prepare(AskRequest("q")).settings.answer_synthesis_mode=="continuous"
    assert service.prepare(AskRequest("q",teaching_generation_mode="v3")).settings.answer_synthesis_mode=="planned"


def test_retry_only_before_visible_text_and_unknown_usage():
    req,value=inputs()
    first=Client([StreamEvent("error",text="network",retryable=True)])
    second=Client([StreamEvent("content",text="正文 [E1]"),StreamEvent("finish",finish_reason="stop")])
    choices=iter([first,second]);events=[]
    result=ContinuousAnswerSynthesizer(lambda:next(choices)).synthesize(req,value,on_event=events.append)
    assert result.trace["stream_retry_count"]==1
    starts=[e for e in events if e.type=="answer.generation_started"]
    assert len({e.generation_id for e in starts})==2
    assert result.trace["input_tokens"] is None and not result.trace["usage_complete"]
    interrupted=Client([StreamEvent("content",text="已到正文"),StreamEvent("error",text="network",retryable=True)])
    result=ContinuousAnswerSynthesizer(lambda:interrupted).synthesize(req,value)
    assert len(interrupted.calls)==1 and result.trace["completion_status"]=="partial"


def test_deadline_cancel_and_delivery_failure_do_not_retry():
    req,value=inputs()
    client=Client([])
    with request_deadline(.001,started=0):
        result=ContinuousAnswerSynthesizer(lambda:client).synthesize(req,value)
    assert result.trace["completion_status"]=="failed" and not client.calls
    token=CancellationToken();token.cancel()
    with pytest.raises(RequestCancelled),execution_scope(token):
        ContinuousAnswerSynthesizer(lambda:client).synthesize(req,value)
    def broken(event):raise RuntimeError("storage")
    with pytest.raises(DeliveryError):
        ContinuousAnswerSynthesizer(lambda:client).synthesize(req,value,on_event=broken)
    assert not client.calls


@pytest.mark.parametrize("finish",["length",None])
def test_abnormal_finish_retains_draft_without_promoting_success(finish):
    req,value=inputs();events=[]
    client=Client([StreamEvent("content",text="未完正文"),StreamEvent("finish",finish_reason=finish)])
    result=ContinuousAnswerSynthesizer(lambda:client).synthesize(req,value,on_event=events.append)
    assert result.trace["completion_status"]=="partial" and result.answer.answer==""
    assert not any(e.type=="answer.section_upsert" for e in events)


def test_redactor_tail_precedes_commit_and_cannot_follow_terminal(tmp_path,capsys):
    store=RunStore(tmp_path/"db")
    rid=store.create_run(store.create_session()["id"],"q","fast","id")["id"]
    sink=lambda event:store.answer_event(rid,event)
    buffer=BufferedDelivery(sink,secrets=("secret-key",))
    events=[AnswerEvent("answer.generation_started","g",{}),
        AnswerEvent("answer.section_started","g",{"section_id":"S1"}),
        AnswerEvent("answer.text_delta","g",{"section_id":"S1","delta":"正文 secret"}),
        AnswerEvent("answer.section_upsert","g",{"section_id":"S1","markdown":"正式正文","citations":[]}),
        AnswerEvent("answer.generation_finished","g",{"status":"complete"})]
    for event in events:buffer.submit(event)
    buffer.close()
    assert store.run(rid)["answer"]=="正式正文"
    received=store.events(rid)
    assert received[-2]["type"]=="answer.section_upsert" and received[-1]["type"]=="answer.generation_finished"
    printer=EventPrinter(Settings(_env_file=None,deepseek_api_key="secret-key"))
    for event in events:printer(event)
    values=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert values[-2]["type"]=="answer.section_upsert"
    assert "secret" not in "".join(v["data"].get("delta","") for v in values)
