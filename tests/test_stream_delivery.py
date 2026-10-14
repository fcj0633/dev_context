import io
import json
import random
import sqlite3
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from devcontext.application import AskRequest, AskService
from devcontext.config import Settings
from devcontext.delivery import AnswerEvent, DeliveryError, Generation, INPUT_CITATIONS
from devcontext.delivery.buffer import BufferedDelivery, StreamRedactor
from devcontext.execution import CancellationToken, RequestCancelled, execution_scope
from devcontext.explanation.stream_writer import SectionParser, validate_section
from devcontext.web.store import RunStore


@pytest.mark.parametrize("seed", range(30))
def test_arbitrary_boundaries_emit_body_before_end_without_leaking_markers(seed):
    text = "<<<SECTION:S1>>>\n## 标题\n中文🙂正文 [E1]\n<<<END_SECTION:S1>>>"
    updates = []
    parser = SectionParser(["S1"], on_update=lambda *args: updates.append(args))
    rng = random.Random(seed)
    cut = text.index("<<<END_SECTION")
    i = 0
    while i < cut:
        n = min(cut, i+rng.randint(1, 8))
        assert list(parser.feed(text[i:n])) == []
        i = n
    assert "中文🙂正文" in "".join(t for k, _, t in updates if k == "delta")
    complete = []
    while i < len(text):
        n = min(len(text), i+rng.randint(1, 8))
        complete.extend(parser.feed(text[i:n])); i = n
    parser.finish()
    assert complete == [("S1", "## 标题\n中文🙂正文 [E1]")]
    assert "<<<" not in "".join(t for k, _, t in updates if k == "delta")
    assert ">>>" not in "".join(t for k, _, t in updates if k == "delta")


def test_input_scope_normalizes_title_without_adding_model_calls():
    section = SimpleNamespace(id="S1", title="正确标题", evidence_labels=("E1",))
    warnings = []
    value = validate_section(section, "## 不同标题\n有正文 [E2] [E999]", {"E1", "E2"},
        permissive=True, warnings=warnings, citation_policy=INPUT_CITATIONS)
    assert value.markdown == "## 正确标题\n有正文 [E2] "
    assert value.citations == ("E2",)
    assert len(warnings) == 2
    old = validate_section(section, "## 正确标题\n有正文 [E2]", {"E1", "E2"}, permissive=True)
    assert old.citations == ()


@pytest.mark.parametrize("cut", range(1, 19))
def test_secret_and_connection_redaction_survive_chunk_boundaries(cut):
    text = "正常🙂 secret-key-123456 postgres://user:pass@host/db 继续"
    redactor = StreamRedactor(["secret-key-123456"])
    outputs = [redactor.feed(text[i:i+cut]) for i in range(0,len(text),cut)]
    result = "".join(outputs)
    assert "secret-key" not in result and "user:pass" not in result and "postgres:" not in result
    assert "正常🙂" in result and "继续" in result


def test_timed_flush_does_not_wait_for_next_model_token():
    delivered, ready = [], Event()
    def sink(e):
        delivered.append(e)
        if e.data.get("delta") == "正文": ready.set()
    buffer = BufferedDelivery(sink)
    try:
        buffer.submit(AnswerEvent("answer.text_delta", "g", {"section_id":"S1", "delta":"## 标题\n"}))
        buffer.submit(AnswerEvent("answer.text_delta", "g", {"section_id":"S1", "delta":"正文"}))
        assert ready.wait(1)
        assert "".join(e.data["delta"] for e in delivered) == "## 标题\n正文"
    finally:
        buffer.close()
    assert not buffer.worker.is_alive() and buffer.queue.maxsize == 64


def test_bounded_delivery_applies_backpressure_without_losing_text():
    entered, release = Event(), Event()
    values = []
    def sink(e):
        entered.set(); assert release.wait(3); values.append(e.data["delta"])
    buffer = BufferedDelivery(sink)
    done = Event()
    def produce():
        for i in range(150):
            buffer.submit(AnswerEvent("answer.text_delta","g",{"section_id":"S1","delta":str(i)+","}))
        done.set()
    producer = Thread(target=produce)
    producer.start()
    assert entered.wait(1)
    assert not done.wait(.1)
    assert buffer.queue.qsize() <= 64
    release.set(); producer.join(3); buffer.close()
    assert done.is_set()
    assert "".join(values) == "".join(str(i)+"," for i in range(150))


def test_delivery_error_escapes_model_recovery_and_closes_worker():
    def broken(e): raise RuntimeError("private provider/db detail")
    buffer = BufferedDelivery(broken)
    buffer.submit(AnswerEvent("answer.generation_started","g",{}))
    with pytest.raises(DeliveryError): buffer.drain()
    with pytest.raises(DeliveryError): buffer.close()
    assert not buffer.worker.is_alive()
    def workflow(*args):
        def run(*args, **kwargs):
            Generation("test")
        return SimpleNamespace(run=run)
    with pytest.raises(DeliveryError):
        AskService(Settings(_env_file=None),workflow_factory=workflow).ask(AskRequest("q"),on_event=broken)


def run(store):
    return store.create_run(store.create_session()["id"],"q","fast","request")["id"]


def send(store, rid, kind, gen="g1", **data):
    return store.answer_event(rid, AnswerEvent(kind,gen,data))


def test_store_retry_commit_terminal_and_restart_preserve_authoritative_state(tmp_path):
    store = RunStore(tmp_path/"db"); rid = run(store)
    send(store,rid,"answer.generation_started")
    send(store,rid,"answer.section_started",section_id="S1")
    send(store,rid,"answer.text_delta",section_id="S1",delta="旧预览")
    send(store,rid,"answer.generation_finished",status="failed")
    send(store,rid,"answer.generation_started",gen="g2")
    assert store.run(rid)["drafts"] == []
    assert send(store,rid,"answer.text_delta",section_id="S1",delta="晚到") is None
    send(store,rid,"answer.section_started",gen="g2",section_id="S1")
    send(store,rid,"answer.text_delta",gen="g2",section_id="S1",delta="新预览")
    send(store,rid,"answer.section_upsert",gen="g2",section_id="S1",markdown="正式正文",citations=[])
    assert store.run(rid)["answer"] == "正式正文" and store.run(rid)["drafts"] == []
    send(store,rid,"answer.section_started",gen="g2",section_id="S2")
    send(store,rid,"answer.text_delta",gen="g2",section_id="S2",delta="未完成")
    resumed = RunStore(tmp_path/"db"); resumed.recover()
    state = resumed.run(rid)
    assert state["status"] == "partial" and state["answer"] == "正式正文"
    assert state["drafts"][0]["text"] == "未完成" and state["drafts"][0]["state"] == "interrupted"
    assert send(store,rid,"answer.text_delta",gen="g2",section_id="S2",delta="晚到") is None
    events=store.events(rid,limit=1000)
    assert [e["seq"] for e in events] == list(range(1,len(events)+1))


def test_draft_does_not_promote_failure_to_success_and_transaction_is_atomic(tmp_path,monkeypatch):
    store = RunStore(tmp_path/"db"); rid=run(store)
    send(store,rid,"answer.generation_started");send(store,rid,"answer.section_started",section_id="S1")
    original=store._event
    def broken(*args): raise sqlite3.OperationalError("write failed")
    monkeypatch.setattr(store,"_event",broken)
    with pytest.raises(sqlite3.OperationalError): send(store,rid,"answer.text_delta",section_id="S1",delta="lost")
    assert store.run(rid)["drafts"][0]["text"] == ""
    monkeypatch.setattr(store,"_event",original)
    send(store,rid,"answer.text_delta",section_id="S1",delta="草稿")
    store.finish(rid,"failed")
    value=store.run(rid)
    assert value["status"] == "failed" and value["answer"] == ""
    assert value["drafts"][0]["text"] == "草稿"


def test_v1_migration_creates_backup_and_retains_history(tmp_path):
    path=tmp_path/"db";store=RunStore(path);rid=run(store)
    store.section(rid,{"section_id":"S1","markdown":"旧记录","citations":[]})
    with sqlite3.connect(path) as db:
        db.executescript("DROP TABLE run_drafts; DROP TABLE run_generations; PRAGMA user_version=1;")
    migrated=RunStore(path)
    assert migrated.run(rid)["answer"] == "旧记录" and migrated.run(rid)["drafts"] == []
    backups=list(tmp_path.glob("*.bak")); assert len(backups)==1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    RunStore(path); assert len(list(tmp_path.glob("*.bak")))==1


def test_cancel_during_silent_curl_read_stops_reader(monkeypatch):
    import devcontext.llm.streaming as module
    killed=Event()
    class Output:
        def read1(self,n): killed.wait(3); return b""
        def close(self): pass
    class Process:
        stdin=io.BytesIO(); stdout=Output()
        def poll(self): return 0 if killed.is_set() else None
        def kill(self): killed.set()
        def wait(self,**kw): return 0
    monkeypatch.setattr(module.shutil,"which",lambda name:"curl")
    monkeypatch.setattr(module.subprocess,"Popen",lambda *a,**kw:Process())
    token=CancellationToken(); completed=Event(); errors=[]
    client=SimpleNamespace(model="test",api_key="key",base_url="http://unused",max_tokens=100,
                           timeout_seconds=10,reasoning_effort=None,json_mode=False)
    from devcontext.llm.client import LLMMessage
    def consume():
        try:
            with execution_scope(token): list(module.curl_stream(client,[LLMMessage("user","q")]))
        except BaseException as exc: errors.append(exc)
        finally: completed.set()
    thread=Thread(target=consume);thread.start();time.sleep(.05);token.cancel()
    assert completed.wait(1);thread.join()
    assert killed.is_set() and isinstance(errors[0],RequestCancelled)


@pytest.mark.parametrize("flags",[["--debug"],["--repeat","2"],["--plan-only"],["--perf"],["--no-plan"],["--answer-mode","legacy"]])
def test_cli_event_mode_rejects_conflicts_before_models(flags,capsys):
    from devcontext.cli import main
    assert main(["ask","q","--stream-events",*flags])==1
    assert "ERROR" in capsys.readouterr().err


def test_cli_emits_only_ndjson_and_masks_split_credentials(monkeypatch,capsys):
    import devcontext.cli as cli
    def workflow(*args):
        def run(*args,**kwargs):
            generation=Generation("test")
            generation.parser_update("start","S1")
            generation.parser_update("delta","S1","secret-")
            generation.parser_update("delta","S1","key 正文")
            generation.finish("failed")
            return SimpleNamespace(trace=SimpleNamespace(teaching={"completion_status":"failed"},evidence_package_state="PARTIAL"),
                                   answer_result=SimpleNamespace(answer=""))
        return SimpleNamespace(run=run)
    monkeypatch.setenv("DEEPSEEK_API_KEY","secret-key")
    monkeypatch.setattr(cli,"AskService",lambda settings:AskService(settings,workflow_factory=workflow))
    assert cli.main(["ask","q","--stream-events"])==1
    output=capsys.readouterr().out
    assert "secret-key" not in output and "secret-" not in output
    values=[json.loads(line) for line in output.splitlines()]
    assert values[-1]["type"]=="run.finished"
    assert [v["seq"] for v in values]==list(range(1,len(values)+1))


def test_delivery_flushes_a_possible_secret_suffix_as_a_redaction():
    values=[]
    buffer=BufferedDelivery(values.append,secrets=("secret-key",))
    buffer.submit(AnswerEvent("answer.generation_started","g",{}))
    buffer.submit(AnswerEvent("answer.section_started","g",{"section_id":"S1"}))
    buffer.submit(AnswerEvent("answer.text_delta","g",{"section_id":"S1","delta":"正文 secret"}))
    buffer.submit(AnswerEvent("answer.generation_finished","g",{"status":"complete"}))
    buffer.close()
    body="".join(event.data.get("delta","") for event in values)
    assert "secret" not in body and "[已隐藏]" in body


def test_real_writer_retry_uses_new_generation_and_single_commit():
    from test_single_stream import plan_for, FakeStream, section_text
    from devcontext.explanation.stream_writer import SingleStreamingTeachingWriter
    from devcontext.llm.client import StreamEvent
    from test_teaching_workflow import make_package
    plan=plan_for(count=1)
    first=FakeStream([StreamEvent("content",text="<<<SECTION:S1>>>\n## 问题1\n旧正文"),
                      StreamEvent("error",text="interrupted",retryable=True)])
    second=FakeStream([StreamEvent("content",text=section_text(plan.sections[0])),StreamEvent("finish",finish_reason="stop")])
    clients=iter([first,second]); events=[]; callbacks=[]
    with execution_scope(on_event=events.append):
        sections,trace=SingleStreamingTeachingWriter(lambda:next(clients)).write("q",plan,make_package().context_bundle,
            SimpleNamespace(max_output_tokens=1000),request_started=time.perf_counter(),on_section=callbacks.append)
    starts=[e for e in events if e.type=="answer.generation_started"]
    assert len(starts)==2 and starts[0].generation_id != starts[1].generation_id
    assert len(callbacks)==len(sections)==1
    assert len([e for e in events if e.type=="answer.section_upsert"])==1
    assert trace["completion_status"]=="complete"


def test_full_direct_fallback_emits_incrementally_and_cleans_sources():
    from devcontext.explanation.v3.fallback import write_direct
    from devcontext.context.budget import FALLBACK_CAPABILITIES
    from devcontext.context.estimator import HeuristicTokenEstimator
    from devcontext.request import UserRequest, AnswerOptions
    from test_teaching_workflow import make_package
    from test_single_stream import FakeStream
    from devcontext.llm.client import StreamEvent
    events=[]
    def chunks():
        yield StreamEvent("content",text="正文 [E2] [E999]")
        assert any(e.type=="answer.text_delta" for e in events)
        assert not any(e.type=="answer.section_upsert" for e in events)
        yield StreamEvent("finish",finish_reason="stop")
    client=FakeStream([])
    client.generate_stream=lambda messages:chunks()
    with execution_scope(on_event=events.append):
        sections,trace,_=write_direct(lambda:client,UserRequest("q",AnswerOptions("teach")),make_package(),None,
            FALLBACK_CAPABILITIES,HeuristicTokenEstimator(),started=time.perf_counter(),on_section=None)
    assert trace["completion_status"]=="complete"
    assert "[E2]" in sections[0].markdown and "E999" not in sections[0].markdown
    assert events[-1].type=="answer.generation_finished"


def test_web_preview_disable_uses_existing_section_delivery(tmp_path):
    from fastapi.testclient import TestClient
    from devcontext.web.api import create_app
    from test_web_v1 import Service, completed
    settings=Settings(_env_file=None).model_copy(update={"web_stream_preview_enabled":False})
    with TestClient(create_app(database_path=tmp_path/"db",settings=settings,service=Service())) as client:
        sid=client.post('/api/v1/sessions',json={}).json()['id']
        rid=client.post(f'/api/v1/sessions/{sid}/runs',json={'query':'q','client_request_id':'id'}).json()['id']
        value=completed(client,rid)
        assert value['status']=='completed' and value['drafts']==[]
        assert value['sections']
