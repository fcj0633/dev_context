"""Incremental SSE transport; provider reasoning never leaves this module."""
from __future__ import annotations

import codecs
import json
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from devcontext.llm.client import LLMMessage, StreamEvent
from devcontext.observability.recorder import record_llm_call


class StreamFailure(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable
        self.recoverable = retryable


def parse_sse(chunks: Iterable[bytes]) -> Iterator[StreamEvent]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    data: list[str] = []
    finish_reason = None
    done = False

    def dispatch(payload: str) -> Iterator[StreamEvent]:
        nonlocal finish_reason, done
        if payload == "[DONE]":
            if finish_reason != "stop":
                raise StreamFailure(f"stream did not finish normally: {finish_reason}")
            done = True
            return
        if done:
            raise StreamFailure("SSE data after DONE")
        try:
            value = json.loads(payload)
            if "error" in value:
                raise StreamFailure("provider stream error")
            usage = value.get("usage")
            if isinstance(usage, dict):
                counts = {k: v for k, v in usage.items() if type(v) is int}
                details = usage.get("completion_tokens_details", {})
                if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
                    counts["reasoning_tokens"] = details["reasoning_tokens"]
                yield StreamEvent("usage", usage=counts)
            for choice in value.get("choices", []):
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    if not isinstance(content, str):
                        raise StreamFailure("invalid content delta")
                    yield StreamEvent("content", text=content)
                # reasoning_content is intentionally neither saved nor emitted.
                reason = choice.get("finish_reason")
                if reason is not None:
                    if finish_reason is not None:
                        raise StreamFailure("duplicate finish event")
                    finish_reason = reason
                    yield StreamEvent("finish", finish_reason=reason)
        except (ValueError, TypeError, AttributeError) as exc:
            raise StreamFailure("invalid SSE payload") from exc

    for chunk in chunks:
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if data:
                    yield from dispatch("\n".join(data))
                    data.clear()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif line.startswith((":", "event:", "id:", "retry:")):
                continue
            else:
                raise StreamFailure("invalid SSE framing")
    buffer += decoder.decode(b"", final=True)
    if buffer.strip() or data or not done:
        raise StreamFailure("stream interrupted before DONE")


def curl_stream(client, messages: Sequence[LLMMessage]) -> Iterator[bytes]:
    if not messages:
        raise StreamFailure("messages must not be empty", retryable=False)
    executable = shutil.which("curl.exe") or shutil.which("curl")
    if executable is None:
        raise StreamFailure("curl is unavailable", retryable=False)
    body = {
        "model": client.model,
        "messages": [message.to_dict() for message in messages],
        getattr(client, "output_token_parameter", "max_tokens"): client.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if client.reasoning_effort is not None:
        body["reasoning_effort"] = client.reasoning_effort
    if client.json_mode:
        body["response_format"] = {"type": "json_object"}
    # Credentials remain on stdin, never in a command line or diagnostic.
    config = "\n".join([
        f'url = "{client.base_url}/chat/completions"',
        'request = "POST"',
        f'header = "Authorization: Bearer {client.api_key}"',
        'header = "Content-Type: application/json"',
        "silent", "show-error",
    ])
    with tempfile.TemporaryDirectory(prefix="devcontext-stream-") as directory:
        root = Path(directory)
        request = root / "request.json"
        headers = root / "headers.txt"
        request.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        with (root / "stderr.txt").open("wb") as stderr:
            process = subprocess.Popen(
                [executable, "--config", "-", "--no-buffer", "--max-time",
                 str(client.timeout_seconds), "--dump-header", str(headers),
                 "--data-binary", f"@{request}"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
            )
            chunks: queue.Queue[bytes | None] = queue.Queue()

            def read_output():
                try:
                    while chunk := process.stdout.read1(4096):
                        chunks.put(chunk)
                finally:
                    chunks.put(None)

            worker = threading.Thread(target=read_output, daemon=True)
            deadline = time.perf_counter() + client.timeout_seconds
            def check_status():
                status_codes = re.findall(r"HTTP/\S+\s+(\d{3})", headers.read_text(encoding="utf-8")) if headers.exists() else []
                status = int(status_codes[-1]) if status_codes else 0
                if not 200 <= status < 300:
                    from devcontext.llm.errors import http_failure
                    raise http_failure(f"stream request failed (HTTP {status})", status)
            try:
                process.stdin.write(config.encode("utf-8"))
                process.stdin.close()
                worker.start()
                while True:
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise StreamFailure("stream request timed out")
                    try:
                        chunk = chunks.get(timeout=remaining)
                    except queue.Empty as exc:
                        raise StreamFailure("stream request timed out") from exc
                    if chunk is None:
                        check_status()
                        break
                    check_status()
                    yield chunk
                code = process.wait(timeout=max(0.1, deadline - time.perf_counter()))
                if code != 0:
                    raise StreamFailure(f"curl stream failed (exit {code})")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if worker.ident is not None:
                    worker.join(timeout=2)
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()


def generate_stream(client, messages: Sequence[LLMMessage]) -> Iterator[StreamEvent]:
    from devcontext.llm.errors import check_fatal_request
    check_fatal_request()
    from devcontext.deadline import bounded_timeout
    original_timeout = client.timeout_seconds
    client.timeout_seconds = bounded_timeout(original_timeout)
    started = time.perf_counter()
    client.last_usage = {}
    client.last_finish_reason = None
    success = False
    error = None
    source = curl_stream(client, messages)
    prefix = None
    if getattr(client, "provider", None) == "openai":
        from devcontext.llm.openai_compatible import ReasoningPrefixFilter
        prefix = ReasoningPrefixFilter()
    try:
        for event in parse_sse(source):
            if prefix is not None and event.type == "content":
                text = prefix.feed(event.text)
                if text:
                    yield StreamEvent("content", text=text)
                continue
            if prefix is not None and event.type == "finish":
                tail = prefix.finish()
                if tail:
                    yield StreamEvent("content", text=tail)
            if event.type == "usage":
                client.last_usage.update(event.usage or {})
            elif event.type == "finish":
                client.last_finish_reason = event.finish_reason
            yield event
        success = True
    except Exception as exc:
        error = str(exc).replace(client.api_key, "[redacted]")
        yield StreamEvent("error", text=error, retryable=getattr(exc, "retryable", True))
    finally:
        source.close()
        client.timeout_seconds = original_timeout
        client.last_latency_ms = (time.perf_counter() - started) * 1000
        record_llm_call(
            latency_ms=client.last_latency_ms, model=client.model,
            reasoning_effort=client.reasoning_effort, max_tokens=client.max_tokens,
            input_tokens=client.last_usage.get("prompt_tokens"),
            output_tokens=client.last_usage.get("completion_tokens"),
            reasoning_tokens=client.last_usage.get("reasoning_tokens"),
            finish_reason=client.last_finish_reason, success=success,
            error=error or (None if success else "stream consumer stopped early"),
        )
