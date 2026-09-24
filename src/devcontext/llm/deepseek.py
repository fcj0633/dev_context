from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence

from devcontext.llm.client import LLMMessage


class DeepSeekLLMClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
        reasoning_effort: str = "low",
        max_tokens: int = 4096,
        timeout_seconds: int = 120,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        if not base_url.strip():
            raise ValueError("DEEPSEEK_BASE_URL must not be empty")
        if not model.strip():
            raise ValueError("DEEPSEEK_MODEL must not be empty")
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("reasoning_effort must be low, high, or max")
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

    def generate(self, messages: Sequence[LLMMessage]) -> str:
        if not messages:
            raise ValueError("messages must not be empty")
        executable = shutil.which("curl.exe") or shutil.which("curl")
        if executable is None:
            raise RuntimeError("DeepSeek request requires curl, but curl is unavailable")

        body = {
            "model": self.model,
            "messages": [message.to_dict() for message in messages],
            "reasoning_effort": self.reasoning_effort,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
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
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exception:
                raise RuntimeError(
                    f"DeepSeek request timed out after {self.timeout_seconds} seconds"
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
                f"DeepSeek request failed (HTTP {status_code}): {message or 'unknown error'}"
            )
        try:
            payload = json.loads(response_body)
            choice = payload["choices"][0]
            finish_reason = choice["finish_reason"]
            content = choice["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exception:
            raise RuntimeError("DeepSeek returned an invalid response payload") from exception
        if finish_reason != "stop":
            raise RuntimeError(
                f"DeepSeek generation did not complete normally: finish_reason={finish_reason}"
            )
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("DeepSeek returned an empty answer")
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
