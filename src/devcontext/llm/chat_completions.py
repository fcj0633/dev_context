from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence

from devcontext.llm.client import LLMMessage
from devcontext.observability.recorder import record_llm_call
from devcontext.deadline import bounded_timeout


class ChatCompletionsLLMClient:
    provider = "deepseek"
    display_name = "DeepSeek"
    config_prefix = "DEEPSEEK"
    output_token_parameter = "max_tokens"
    # Transport vocabulary only. Actual model capability lives in the factory.
    supported_reasoning_efforts = {"low", "medium", "high", "max"}

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
        reasoning_effort: str | None = "low",
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
        json_mode: bool = False,
    ) -> None:
        if not api_key.strip():
            raise ValueError(f"{self.config_prefix}_API_KEY is not configured")
        if not base_url.strip():
            raise ValueError(f"{self.config_prefix}_BASE_URL must not be empty")
        if not model.strip():
            raise ValueError(f"{self.config_prefix}_MODEL must not be empty")
        if reasoning_effort is not None and reasoning_effort not in self.supported_reasoning_efforts:
            raise ValueError("reasoning_effort must be low, medium, high, max, or None")
        if max_tokens < 1:
            raise ValueError("max_tokens must be greater than 0")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be greater than 0")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.json_mode = json_mode
        self.last_usage: dict[str, int] = {}
        self.last_latency_ms: float = 0.0
        # Kept rather than discarded: "length" is how a call silently burns its
        # whole token allowance, and it is invisible if the value is only ever
        # compared against "stop" and then dropped.
        self.last_finish_reason: str | None = None

    def generate(self, messages: Sequence[LLMMessage]) -> str:
        """Time and trace one call, then delegate.

        The stage label is read from the ambient recorder context rather than
        taken as an argument, so the ``LLMClient`` protocol - and every test
        double that implements it - keeps its single-parameter shape.
        """
        started = time.perf_counter()
        self.last_usage = {}
        self.last_finish_reason = None
        try:
            content = self._generate(messages)
        except Exception as exception:
            from devcontext.agentic.models import error_detail

            record_llm_call(
                latency_ms=(time.perf_counter() - started) * 1000,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                max_tokens=self.max_tokens,
                json_mode=self.json_mode,
                input_tokens=self.last_usage.get("prompt_tokens", self.last_usage.get("input_tokens")),
                output_tokens=self.last_usage.get("completion_tokens", self.last_usage.get("output_tokens")),
                reasoning_tokens=self.last_usage.get("reasoning_tokens"),
                finish_reason=self.last_finish_reason,
                success=False,
                error=error_detail(exception),
            )
            raise
        usage = self.last_usage
        record_llm_call(
            latency_ms=(time.perf_counter() - started) * 1000,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            json_mode=self.json_mode,
            input_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
            output_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
            reasoning_tokens=usage.get("reasoning_tokens"),
            finish_reason=self.last_finish_reason,
            success=True,
        )
        return content

    def generate_stream(self, messages: Sequence[LLMMessage]):
        from devcontext.llm.streaming import generate_stream

        return generate_stream(self, messages)

    def _generate(self, messages: Sequence[LLMMessage]) -> str:
        if not messages:
            raise ValueError("messages must not be empty")
        executable = shutil.which("curl.exe") or shutil.which("curl")
        if executable is None:
            raise RuntimeError(f"{self.display_name} request requires curl, but curl is unavailable")

        body = {
            "model": self.model,
            "messages": [message.to_dict() for message in messages],
            self.output_token_parameter: self.max_tokens,
            "stream": False,
        }
        if self.reasoning_effort is not None:
            body["reasoning_effort"] = self.reasoning_effort
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        config = "\n".join(
            [
                f'url = "{self.base_url}/chat/completions"',
                'request = "POST"',
                f'header = "Authorization: Bearer {self.api_key}"',
                'header = "Content-Type: application/json"',
                "silent",
                "show-error",
            ]
        )
        request_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", delete=False
            ) as request_file:
                json.dump(body, request_file, ensure_ascii=False)
                request_path = request_file.name
            started = time.perf_counter()
            try:
                completed = subprocess.run(
                    [
                        executable,
                        "--config",
                        "-",
                        "--data-binary",
                        f"@{request_path}",
                        "--write-out",
                        "\n__HTTP_STATUS__:%{http_code}",
                    ],
                    input=config,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=bounded_timeout(self.timeout_seconds),
                    check=False,
                )
                self.last_latency_ms = (time.perf_counter() - started) * 1000
            except subprocess.TimeoutExpired as exception:
                raise RuntimeError(
                    f"{self.display_name} request timed out after {self.timeout_seconds} seconds"
                ) from exception
        finally:
            if request_path is not None:
                try:
                    os.unlink(request_path)
                except FileNotFoundError:
                    pass

        response_body, status_code = self._split_response(completed.stdout)
        if completed.returncode != 0 or not 200 <= status_code < 300:
            message = " | ".join(
                value
                for value in (completed.stderr.strip(), response_body.strip())
                if value
            )
            message = message.replace(self.api_key, "[redacted]")
            raise RuntimeError(
                f"{self.display_name} request failed (HTTP {status_code}): {message or 'unknown error'}"
            )
        try:
            payload = json.loads(response_body)
            choice = payload["choices"][0]
            finish_reason = choice["finish_reason"]
            self.last_finish_reason = finish_reason
            content = choice["message"]["content"]
            usage = payload.get("usage", {})
            if isinstance(usage, dict):
                self.last_usage = {
                    key: value for key, value in usage.items() if isinstance(value, int)
                }
                details = usage.get("completion_tokens_details", {})
                if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
                    self.last_usage["reasoning_tokens"] = details["reasoning_tokens"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exception:
            raise RuntimeError(f"{self.display_name} returned an invalid response payload") from exception
        if finish_reason != "stop":
            raise RuntimeError(
                f"{self.display_name} generation did not complete normally: finish_reason={finish_reason}"
            )
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"{self.display_name} returned an empty answer")
        return content.strip()

    @staticmethod
    def _split_response(stdout: str) -> tuple[str, int]:
        marker = "\n__HTTP_STATUS__:"
        if marker not in stdout:
            return stdout, 0
        response_body, raw_status = stdout.rsplit(marker, 1)
        try:
            return response_body, int(raw_status.strip())
        except ValueError:
            return response_body, 0
