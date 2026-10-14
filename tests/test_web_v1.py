import json
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from devcontext.config import Settings
from devcontext.execution import CancellationToken, RequestCancelled, execution_scope
from devcontext.deadline import remaining_seconds
from devcontext.explanation.stream_writer import ValidatedSection
from devcontext.models import Citation, ContextBundle, ContextItem
from devcontext.web.api import create_app
from devcontext.web.store import RunStore
from devcontext.web.manager import RunManager
from devcontext.web.protocol import PublicData, RunError


def bundle():
    item = ContextItem(Citation("E1", "CODE", "Order.java", start_line=3, end_line=7), "void order() {}", 7, "METHOD", 1, 1)
    return ContextBundle("q", [item], "", 15, 100, False)


class Service:
    def __init__(self, generation="complete", state="READY", block=False, fail=False, mismatch=False):
        self.generation, self.state, self.fail, self.mismatch = generation, state, fail, mismatch
        self.block = block
        self.entered, self.release = Event(), Event()
        self.calls = []

    def ask(self, request, **kwargs):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("private-response secret-key postgresql://user:pass@db")
        kwargs["observer"].on_action_completed(SimpleNamespace(source_scope="CODE", round_index=0),
            SimpleNamespace(strategy="hybrid", results=[1], timings=SimpleNamespace(total_ms=3)))
        kwargs["on_evidence"](bundle())
        section = ValidatedSection("S1", "## Answer\n真实回答 [E1]", ("E1",))
        kwargs["on_section"](section)
        kwargs["on_section"](section)
        self.entered.set()
        if self.block:
            self.release.wait(5)
        kwargs["cancellation"].check()
        trace = SimpleNamespace(teaching={"completion_status": self.generation, "raw_response": "private-response"},
            agent_retrieval={}, stage_usage=[], final_coverage=[], evidence_package_state=self.state,
            stop_reason="ready", retry_count=0)
        return SimpleNamespace(trace=trace, answer_result=SimpleNamespace(answer="different" if self.mismatch else section.markdown),
                               context_bundle=bundle(), evidence_catalog=None)


def completed(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = client.get(f"/api/v1/runs/{run_id}").json()
        if not value["worker_active"]:
            return value
        time.sleep(.01)
    raise AssertionError("Run did not finish")


@pytest.fixture
def settings():
    return Settings(_env_file=None, deepseek_api_key="secret-key")


@pytest.mark.parametrize("generation,state,status,reason", [
    ("complete", "READY", "completed", None), ("complete", "PARTIAL", "partial", "evidence_insufficient"),
    ("complete", "EMPTY", "partial", "evidence_insufficient"),
    ("complete", "RETRIEVAL_FAILED", "partial", "retrieval_failed"),
    ("partial", "READY", "partial", "generation_interrupted")])
def test_real_api_run_sections_evidence_replay_and_refresh(tmp_path, settings, generation, state, status, reason):
    service = Service(generation, state)
    app = create_app(database_path=tmp_path / "web.db", settings=settings, service=service)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        session = client.post("/api/v1/sessions", json={}).json()
        payload = {"query": "q", "profile": "full", "client_request_id": "request_1"}
        response = client.post(f"/api/v1/sessions/{session['id']}/runs", json=payload)
        assert response.status_code == 202
        run = completed(client, response.json()["id"])
        assert (run["status"], run["reason"]) == (status, reason)
        assert len(run["sections"]) == 1
        assert run["answer"] == run["sections"][0]["markdown"]
        assert run["evidence"][0]["chunk_id"] == 7
        assert run["evidence"][0]["citation"]["start_line"] == 3
        assert service.calls[0].profile == "full"
        repeated = client.post(f"/api/v1/sessions/{session['id']}/runs", json=payload).json()
        assert repeated["id"] == run["id"] and len(service.calls) == 1
        assert client.post(f"/api/v1/sessions/{session['id']}/runs", json=dict(payload, query="changed")).status_code == 409
        replay = client.get(f"/api/v1/runs/{run['id']}/events").text
        events = [json.loads(line[6:]) for line in replay.splitlines() if line.startswith("data: ")]
        assert [e["seq"] for e in events] == list(range(1, run["last_seq"] + 1))
        assert sum(e["type"] == "answer.section_upsert" for e in events) == 1
        assert "private-response" not in replay
        resumed = client.get(f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": str(run["last_seq"]-1)}).text
        assert resumed.count("data: ") == 1
        assert client.get(f"/api/v1/runs/{run['id']}/events?after=999").status_code == 400
        refreshed = client.get(f"/api/v1/sessions/{session['id']}").json()
        assert len(refreshed["messages"]) == 2 and refreshed["runs"][0]["answer"] == run["answer"]
    with TestClient(create_app(database_path=tmp_path / "web.db", settings=settings, service=service)) as client:
        assert client.get(f"/api/v1/runs/{run['id']}").json()["status"] == status


def test_cancel_retains_worker_capacity_and_no_late_publication(tmp_path, settings):
    service = Service(block=True)
    with TestClient(create_app(database_path=tmp_path / "web.db", settings=settings, service=service, max_workers=1)) as client:
        sid = client.post("/api/v1/sessions", json={}).json()["id"]
        run = client.post(f"/api/v1/sessions/{sid}/runs", json={"query": "q", "client_request_id": "a"}).json()
        assert service.entered.wait(3)
        cancelled = client.post(f"/api/v1/runs/{run['id']}/cancel").json()
        assert cancelled["status"] == "cancelled" and cancelled["worker_active"]
        assert cancelled["sections"] and cancelled["evidence"]
        assert client.post(f"/api/v1/runs/{run['id']}/cancel").json()["last_seq"] == cancelled["last_seq"]
        assert client.delete(f"/api/v1/sessions/{sid}").status_code == 409
        assert client.post(f"/api/v1/sessions/{sid}/runs", json={"query": "q2", "client_request_id": "b"}).status_code == 409
        other = client.post("/api/v1/sessions", json={}).json()["id"]
        assert client.post(f"/api/v1/sessions/{other}/runs", json={"query": "q", "client_request_id": "c"}).status_code == 429
        assert client.get(f"/api/v1/sessions/{other}").json()["messages"] == []
        service.release.set()
        final = completed(client, run["id"])
        assert final["status"] == "cancelled" and final["last_seq"] == cancelled["last_seq"]
        assert client.delete(f"/api/v1/sessions/{sid}").status_code == 204


def test_failure_and_mismatch_are_not_success(tmp_path, settings):
    for service, expected in [(Service(fail=True), "failed"), (Service(mismatch=True), "partial")]:
        with TestClient(create_app(database_path=tmp_path / f"{expected}.db", settings=settings, service=service)) as client:
            sid = client.post("/api/v1/sessions", json={}).json()["id"]
            run = client.post(f"/api/v1/sessions/{sid}/runs", json={"query": "q", "client_request_id": "x"}).json()
            final = completed(client, run["id"])
            assert final["status"] == expected
            serialized = json.dumps(final)
            assert "secret-key" not in serialized and "private-response" not in serialized and "postgresql://" not in serialized


def test_validation_crud_and_unknown_resources(tmp_path, settings):
    with TestClient(create_app(database_path=tmp_path / "web.db", settings=settings, service=Service())) as client:
        assert client.get("/api/v1/runs/missing").status_code == 404
        assert client.post("/api/v1/sessions", json={"title": "  "}).status_code == 422
        sid = client.post("/api/v1/sessions", json={}).json()["id"]
        assert client.patch(f"/api/v1/sessions/{sid}", json={"title": "学习记录"}).json()["title"] == "学习记录"
        assert len(client.get("/api/v1/sessions").json()["items"]) == 1
        for payload in [{"query": " ", "client_request_id": "x"}, {"query": "q", "profile": "invalid", "client_request_id": "x"}]:
            assert client.post(f"/api/v1/sessions/{sid}/runs", json=payload).status_code == 422
        assert client.delete(f"/api/v1/sessions/{sid}").status_code == 204


def test_restart_recovery_and_terminal_immutability(tmp_path):
    store = RunStore(tmp_path / "db")
    sid = store.create_session()["id"]
    empty = store.create_run(sid, "q", "fast", "1")["id"]
    store.release_worker(empty)
    partial = store.create_run(sid, "q", "fast", "2")["id"]
    store.section(partial, {"section_id": "S1", "markdown": "valid", "citations": []})
    store.evidence(partial, [{"label": "E1", "content": "actual"}])
    store.recover()
    assert store.run(empty)["status"] == "failed"
    assert store.run(partial)["status"] == "partial" and store.run(partial)["answer"] == "valid"
    assert store.run(partial)["reason"] == "service_interrupted"
    assert store.finish(partial, "completed") is None
    assert store.section(partial, {"section_id": "S2", "markdown": "late"}) is None
    assert store.run(partial)["evidence"]


def test_second_server_cannot_recover_live_database(tmp_path, settings):
    path = tmp_path / "web.db"
    with TestClient(create_app(database_path=path, settings=settings, service=Service())):
        with pytest.raises(Exception):
            with TestClient(create_app(database_path=path, settings=settings, service=Service())):
                pass


def test_cancellation_is_control_flow_and_secrets_are_scrubbed(settings):
    token = CancellationToken()
    with execution_scope(token):
        token.cancel()
        with pytest.raises(RequestCancelled):
            remaining_seconds()
    assert remaining_seconds() is None
    public = PublicData(settings)
    assert public.clean({"a": ["secret-key"]}) == {"a": ["[已隐藏]"]}


def test_transaction_rolls_back_event_and_section(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "db")
    rid = store.create_run(store.create_session()["id"], "q", "fast", "x")["id"]
    def broken(*args):
        raise RuntimeError("disk failure")
    monkeypatch.setattr(store, "_event", broken)
    with pytest.raises(RuntimeError):
        store.section(rid, {"section_id": "S1", "markdown": "answer", "citations": []})
    assert store.run(rid)["sections"] == [] and store.run(rid)["last_seq"] == 1


def test_simultaneous_duplicates_create_one_run(tmp_path, settings):
    from concurrent.futures import ThreadPoolExecutor
    store = RunStore(tmp_path / "db")
    sid = store.create_session()["id"]
    service = Service(block=True)
    manager = RunManager(store, settings, service)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(manager.submit, sid, "q", "fast", "same") for _ in range(8)]
            values = [f.result() for f in futures]
        assert len({r["id"] for r in values}) == 1
        assert service.entered.wait(2)
        assert len(service.calls) == 1 and len(store.session(sid)["messages"]) == 2
    finally:
        service.release.set()
        manager.close()


def test_storage_failure_never_reports_success_and_restart_recovers(tmp_path, settings, monkeypatch):
    import sqlite3
    store = RunStore(tmp_path / "db")
    service = Service(block=True)
    manager = RunManager(store, settings, service)
    run = manager.submit(store.create_session()["id"], "q", "fast", "x")
    assert service.entered.wait(2)
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk unavailable private configuration")
    with monkeypatch.context() as patch:
        patch.setattr(store, "finish", broken)
        service.release.set()
        deadline = time.monotonic() + 3
        while manager.active and time.monotonic() < deadline:
            time.sleep(.01)
        with pytest.raises(RunError) as failure:
            manager.get(run["id"])
        assert failure.value.code == "storage_unavailable"
        assert store.run(run["id"])["status"] == "running"
    manager.close()
    recovered = RunStore(tmp_path / "db")
    recovered.recover()
    assert recovered.run(run["id"])["status"] == "partial"
