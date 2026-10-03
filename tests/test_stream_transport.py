from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from devcontext.llm.deepseek import DeepSeekLLMClient
from devcontext.llm.client import LLMMessage
from devcontext.observability import capture, llm_stage
from test_single_stream import sse_bytes


@pytest.fixture
def endpoint():
    state = {"status": 200, "payload": sse_bytes(), "delay": 0, "requests": []}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            import json
            state["requests"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(state["status"])
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.flush()
            time.sleep(state["delay"])
            try:
                for i in range(0, len(state["payload"]), 7):
                    self.wfile.write(state["payload"][i:i + 7])
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_curl_stream_and_usage_trace(endpoint, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    client = DeepSeekLLMClient("secret-key", base_url=url)
    with capture() as recorder, llm_stage("teaching_draft", "single_stream"):
        events = list(client.generate_stream([LLMMessage("user", "问题")]))
    assert [e.type for e in events] == ["content", "usage", "finish"]
    assert client.last_usage["reasoning_tokens"] == 3
    assert client.last_finish_reason == "stop"
    assert state["requests"][0]["stream"] is True
    assert state["requests"][0]["stream_options"] == {"include_usage": True}
    assert len(recorder.llm_calls) == 1 and recorder.llm_calls[0].success
    assert recorder.llm_calls[0].reasoning_tokens == 3


@pytest.mark.parametrize("status,retryable", [(401, False), (400, False), (429, True), (500, True)])
def test_http_errors_do_not_leak_body_or_credentials(endpoint, monkeypatch, status, retryable):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    state.update(status=status, payload=b"secret-key private provider error")
    events = list(DeepSeekLLMClient("secret-key", base_url=url).generate_stream([LLMMessage("user", "q")]))
    assert len(events) == 1 and events[0].type == "error"
    assert str(status) in events[0].text and events[0].retryable == retryable
    assert "secret-key" not in repr(events)


def test_timeout_and_early_close_record_failed_call(endpoint, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    state["delay"] = 0.2
    client = DeepSeekLLMClient("secret-key", base_url=url)
    client.timeout_seconds = 0.05
    with capture() as recorder, llm_stage("teaching_draft"):
        events = list(client.generate_stream([LLMMessage("user", "q")]))
    assert events[-1].type == "error" and not recorder.llm_calls[0].success
    state["delay"] = 0
    client.timeout_seconds = 2
    with capture() as recorder, llm_stage("teaching_draft"):
        stream = client.generate_stream([LLMMessage("user", "q")])
        next(stream)
        stream.close()
    assert len(recorder.llm_calls) == 1 and not recorder.llm_calls[0].success
