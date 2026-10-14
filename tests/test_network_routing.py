"""Per-provider routes stay explicit under conflicting ambient proxy settings."""
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from devcontext.network import curl_route_args, curl_route_config, validate_proxy_url


def test_explicit_proxy_overrides_no_proxy_without_changing_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:99")
    monkeypatch.setenv("NO_PROXY", "*")
    import os
    before = dict(os.environ)
    assert curl_route_config(proxy_url="http://user:password@localhost:12334") == [
        'proxy = "http://user:password@localhost:12334"', 'noproxy = ""']
    assert curl_route_config(bypass_proxy=True) == ['proxy = ""', 'noproxy = "*"']
    assert curl_route_config() == []
    assert curl_route_args(proxy_url="http://localhost:12334") == ["--disable"]
    assert curl_route_args(bypass_proxy=True) == ["--disable"]
    assert curl_route_args() == []
    assert dict(os.environ) == before


@pytest.mark.parametrize("url", ["", "ftp://host:1", "http://", "http://host:invalid", "http://host:80/path",
                                "http://host:80\nproxy=evil", "http://host:80?secret=x"])
def test_invalid_proxy_rejected_without_echoing_credentials(url):
    with pytest.raises(ValueError) as caught:
        validate_proxy_url(url)
    if url:
        assert url not in str(caught.value)


def test_proxy_and_bypass_conflict():
    with pytest.raises(ValueError, match="mutually exclusive"):
        curl_route_config(proxy_url="http://localhost:12334", bypass_proxy=True)


@pytest.mark.parametrize("bypass,proxy", [(True, None), (False, "http://user:private@localhost:12334")])
def test_nonstream_llm_curl_route_is_on_stdin(monkeypatch, bypass, proxy):
    from devcontext.llm.chat_completions import ChatCompletionsLLMClient
    from devcontext.llm.client import LLMMessage
    import devcontext.llm.chat_completions as module
    requests = []
    def run(args, **kwargs):
        requests.append((args, kwargs["input"]))
        return SimpleNamespace(returncode=0, stderr="", stdout='{"choices":[{"finish_reason":"stop","message":{"content":"ok"}}]}\n__HTTP_STATUS__:200')
    monkeypatch.setattr(module.subprocess, "run", run)
    client = ChatCompletionsLLMClient("key", bypass_proxy=bypass, proxy_url=proxy)
    assert client.generate([LLMMessage("user", "test")]) == "ok"
    args, config = requests[0]
    assert args[1] == "--disable"
    assert all(line in config for line in curl_route_config(proxy_url=proxy, bypass_proxy=bypass))
    assert "private" not in str(args)


def test_streaming_curl_uses_same_route_and_keeps_credentials_off_command_line(monkeypatch):
    from devcontext.llm.chat_completions import ChatCompletionsLLMClient
    from devcontext.llm.client import LLMMessage
    from devcontext.llm.streaming import curl_stream
    import devcontext.llm.streaming as module
    captured = {}
    class Input(io.BytesIO):
        def close(self):
            captured["config"] = self.getvalue().decode()
            super().close()
    class Process:
        stdin = Input()
        stdout = io.BufferedReader(io.BytesIO(b"content"))
        def poll(self): return 0
        def wait(self, **kwargs): return 0
    def popen(args, **kwargs):
        captured["args"] = args
        Path(args[args.index("--dump-header") + 1]).write_text("HTTP/1.1 200 OK\n", encoding="utf-8")
        return Process()
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    c = ChatCompletionsLLMClient("key", proxy_url="http://user:private@localhost:12334")
    assert b"".join(curl_stream(c, [LLMMessage("user", "test")])) == b"content"
    assert captured["args"][1] == "--disable"
    assert 'noproxy = ""' in captured["config"]
    assert "private" in captured["config"] and "private" not in str(captured["args"])


def test_embedding_sdk_and_curl_ignore_ambient_proxy(monkeypatch):
    from devcontext.embedding.client import BailianEmbeddingClient
    import devcontext.embedding.client as module
    captured = {}
    def sdk(**kwargs):
        captured["sdk"] = kwargs
        return SimpleNamespace()
    def transport(**kwargs):
        captured["transport"] = kwargs
        return object()
    def run(args, **kwargs):
        captured["args"], captured["config"] = args, kwargs["input"]
        return SimpleNamespace(returncode=0, stderr="", stdout='{"data":[{"index":0,"embedding":[1.0]}]}\n__HTTP_STATUS__:200')
    monkeypatch.setenv("HTTPS_PROXY", "http://bad-proxy.invalid:9")
    monkeypatch.setattr(module, "OpenAI", sdk)
    monkeypatch.setattr(module, "DefaultHttpxClient", transport)
    monkeypatch.setattr(module.subprocess, "run", run)
    c = BailianEmbeddingClient("key", "https://embedding.invalid/v1", dimensions=1, transport="curl", bypass_proxy=True)
    assert c.embed_query("test") == [1.0]
    assert captured["transport"] == {"trust_env": False}
    assert 'noproxy = "*"' in captured["config"]
    assert captured["args"][1] == "--disable"


def test_provider_factory_isolates_routes():
    from devcontext.config import Settings
    from devcontext.llm.factory import create_llm_client
    s = Settings(_env_file=None, CHATGPT_API_KEY="test", DEEPSEEK_API_KEY="test",
                 openai_base_url="https://gpt.invalid/v1", openai_proxy_url="http://user:private@localhost:12334",
                 deepseek_bypass_proxy=True, llm_provider="openai")
    gpt = create_llm_client(s)
    ds = create_llm_client(s.model_copy(update={"llm_provider": "deepseek"}))
    assert gpt.proxy_url == "http://user:private@localhost:12334" and not gpt.bypass_proxy
    assert ds.proxy_url is None and ds.bypass_proxy
    assert "private" not in repr(s.openai_proxy_url)
