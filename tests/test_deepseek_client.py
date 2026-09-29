from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import devcontext.llm.deepseek as deepseek_module
from devcontext.llm import DeepSeekLLMClient, LLMMessage


def test_deepseek_client_sends_expected_payload_and_cleans_temp_file(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        request_argument = next(value for value in arguments if value.startswith("@"))
        request_path = Path(request_argument[1:])
        captured["request_path"] = request_path
        captured["payload"] = json.loads(request_path.read_text(encoding="utf-8"))
        captured["config"] = kwargs["input"]
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"choices":[{"finish_reason":"stop",'
                '"message":{"content":"基于证据回答 [C1]。"}}]}'
                "\n__HTTP_STATUS__:200"
            ),
            stderr="",
        )

    monkeypatch.setattr(deepseek_module.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(deepseek_module.subprocess, "run", fake_run)
    client = DeepSeekLLMClient(
        api_key="test-secret",
        base_url="https://api.deepseek.example/",
    )

    answer = client.generate([LLMMessage(role="user", content="问题")])

    assert answer == "基于证据回答 [C1]。"
    assert captured["payload"] == {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "问题"}],
        "reasoning_effort": "low",
        "max_tokens": 4096,
        "stream": False,
    }
    assert 'url = "https://api.deepseek.example/chat/completions"' in str(
        captured["config"]
    )
    assert not Path(captured["request_path"]).exists()


def test_deepseek_client_reports_http_error_without_exposing_key(monkeypatch) -> None:
    monkeypatch.setattr(deepseek_module.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(
        deepseek_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                '{"error":{"message":"bad request for do-not-leak"}}'
                "\n__HTTP_STATUS__:400"
            ),
            stderr="",
        ),
    )
    client = DeepSeekLLMClient(api_key="do-not-leak")

    with pytest.raises(RuntimeError) as captured:
        client.generate([LLMMessage(role="user", content="问题")])

    assert "HTTP 400" in str(captured.value)
    assert "do-not-leak" not in str(captured.value)
    assert "[redacted]" in str(captured.value)


def test_deepseek_client_enables_json_output_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> SimpleNamespace:
        request_argument = next(value for value in arguments if value.startswith("@"))
        captured.update(json.loads(Path(request_argument[1:]).read_text(encoding="utf-8")))
        return SimpleNamespace(
            returncode=0,
            stdout='{"choices":[{"finish_reason":"stop","message":{"content":"{}"}}],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n__HTTP_STATUS__:200',
            stderr="",
        )

    monkeypatch.setattr(deepseek_module.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(deepseek_module.subprocess, "run", fake_run)
    client = DeepSeekLLMClient(api_key="secret", json_mode=True)

    client.generate([LLMMessage(role="user", content="json")])

    assert captured["response_format"] == {"type": "json_object"}
    assert client.last_usage == {"prompt_tokens": 7, "completion_tokens": 2}


def test_deepseek_client_rejects_invalid_or_empty_response(monkeypatch) -> None:
    monkeypatch.setattr(deepseek_module.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(
        deepseek_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"choices":[]}\n__HTTP_STATUS__:200',
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="invalid response payload"):
        DeepSeekLLMClient(api_key="secret").generate(
            [LLMMessage(role="user", content="问题")]
        )


def test_deepseek_client_rejects_incomplete_generation(monkeypatch) -> None:
    monkeypatch.setattr(deepseek_module.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(
        deepseek_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                '{"choices":[{"finish_reason":"length",'
                '"message":{"content":"partial"}}]}'
                "\n__HTTP_STATUS__:200"
            ),
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="finish_reason=length"):
        DeepSeekLLMClient(api_key="secret").generate(
            [LLMMessage(role="user", content="问题")]
        )
