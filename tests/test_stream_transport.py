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


@pytest.mark.parametrize("status", [401, 402])
def test_empty_auth_or_balance_failure_is_not_retried(endpoint, monkeypatch, status):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    state.update(status=status, payload=b"")
    events = list(DeepSeekLLMClient("secret-key", base_url=url).generate_stream([LLMMessage("user", "q")]))
    assert events[-1].type == "error" and not events[-1].retryable
    assert str(status) in events[-1].text


def test_openai_gateway_embedded_reasoning_is_not_published(endpoint, monkeypatch):
    import json
    from devcontext.llm.openai_compatible import OpenAICompatibleLLMClient
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    chunks = ['<thi', 'nk>private', ' reasoning</thi', 'nk>', 'answer <think>literal</think>']
    frames = ["data: " + json.dumps({"choices": [{"delta": {"content": chunk}, "finish_reason": None}]}) + "\n\n" for chunk in chunks]
    frames.append('data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
    frames.append('data: [DONE]\n\n')
    state['payload'] = ''.join(frames).encode()
    client = OpenAICompatibleLLMClient('secret-key', base_url=url)
    with capture() as recorder:
        events = list(client.generate_stream([LLMMessage('user', 'q')]))
    assert ''.join(e.text for e in events if e.type == 'content') == 'answer <think>literal</think>'
    assert events[-1].type == 'finish' and recorder.llm_calls[0].success
    assert state['requests'][0]['max_completion_tokens'] == 4096
    assert 'max_tokens' not in state['requests'][0]


def test_openai_unfinished_reasoning_records_failed_stream(endpoint, monkeypatch):
    from devcontext.llm.openai_compatible import OpenAICompatibleLLMClient
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    state['payload'] = b'data: {"choices":[{"delta":{"content":"<think>private"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    with capture() as recorder:
        events = list(OpenAICompatibleLLMClient('secret-key', base_url=url).generate_stream([LLMMessage('user', 'q')]))
    assert [e.type for e in events] == ['error']
    assert not recorder.llm_calls[0].success


def test_openai_json_uses_sse_but_returns_one_complete_result_and_trace(endpoint, monkeypatch):
    from devcontext.llm.openai_compatible import OpenAICompatibleLLMClient
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    state['payload'] = b'data: {"choices":[{"delta":{"content":"<think>reason</think>"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{"content":"{\\\"status\\\":\\\"OK\\\"}"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":8}}\n\ndata: [DONE]\n\n'
    client = OpenAICompatibleLLMClient('secret-key', base_url=url, json_mode=True)
    with capture() as recorder:
        result = client.generate([LLMMessage('user', 'q')])
    assert result == '{"status":"OK"}'
    assert len(recorder.llm_calls) == 1 and recorder.llm_calls[0].success
    assert recorder.llm_calls[0].output_tokens == 8
    assert state['requests'][0]['stream'] is True
    assert state['requests'][0]['response_format'] == {'type':'json_object'}


def test_json_stream_with_complete_json_but_missing_done_is_not_success(endpoint, monkeypatch):
    import json
    from devcontext.llm.openai_compatible import OpenAICompatibleLLMClient
    from devcontext.llm.streaming import StreamFailure
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    url, state = endpoint
    frame = {'choices': [{'delta': {'content': '<think>private</think>{"status":"OK"}'}, 'finish_reason': None}]}
    state['payload'] = ('data: ' + json.dumps(frame) + '\n\n' + 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n').encode()
    client = OpenAICompatibleLLMClient('secret-key', base_url=url, json_mode=True)
    with capture() as recorder, pytest.raises(StreamFailure, match='before DONE'):
        client.generate([LLMMessage('user', 'q')])
    assert client.last_partial_response == '{"status":"OK"}'
    assert len(recorder.llm_calls) == 1 and not recorder.llm_calls[0].success
