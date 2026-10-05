from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from collections.abc import Callable

from openai import APIConnectionError, OpenAI

from devcontext.observability import record_embedding_call
from devcontext.deadline import bounded_timeout, remaining_seconds


def _record_embedding(
    client: "BailianEmbeddingClient",
    query: str,
    started: float,
    *,
    success: bool = True,
    error: str | None = None,
) -> None:
    """Record one query embedding.

    Only the query path is recorded. Ingestion embeds thousands of batches and
    is not part of any answer's latency, so recording there would bury the signal.
    """
    record_embedding_call(
        query=query,
        latency_ms=(time.perf_counter() - started) * 1000,
        batch_size=1,
        model=client.model,
        dimensions=client.dimensions,
        transport=client.effective_transport,
        success=success,
        error=error,
    )


class BailianEmbeddingClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "text-embedding-v4",
        dimensions: int = 1024,
        batch_size: int = 10,
        transport: str = "auto",
    ) -> None:
        if batch_size < 1 or batch_size > 10:
            raise ValueError("text-embedding-v4 batch_size must be between 1 and 10")
        if transport not in {"auto", "openai", "curl"}:
            raise ValueError("transport must be auto, openai, or curl")
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=30.0,
            max_retries=3,
        )
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self._curl_fallback = transport == "curl"

    def _batches(self, values: Sequence[str]) -> Iterable[Sequence[str]]:
        for start in range(0, len(values), self.batch_size):
            yield values[start : start + self.batch_size]

    def embed_documents(
        self,
        texts: Sequence[str],
        on_batch: Callable[[int, list[list[float]]], None] | None = None,
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch_number, batch in enumerate(self._batches(texts), start=1):
            try:
                if self._curl_fallback:
                    try:
                        batch_vectors = self._embed_with_curl(batch)
                    except RuntimeError as exception:
                        if "already running" not in str(exception).lower() or len(batch) < 2:
                            raise
                        middle = len(batch) // 2
                        batch_vectors = self._embed_with_curl(batch[:middle])
                        batch_vectors.extend(self._embed_with_curl(batch[middle:]))
                else:
                    client = self.client
                    remaining = remaining_seconds()
                    if remaining is not None:
                        client = self.client.with_options(timeout=min(30.0, remaining), max_retries=0)
                    response = client.embeddings.create(
                        model=self.model,
                        input=list(batch),
                        dimensions=self.dimensions,
                        encoding_format="float",
                    )
                    ordered = sorted(response.data, key=lambda item: item.index)
                    batch_vectors = [item.embedding for item in ordered]
            except APIConnectionError:
                if self.transport == "openai":
                    raise
                self._curl_fallback = True
                batch_vectors = self._embed_with_curl(batch)
            except Exception as exception:
                lengths = [len(text) for text in batch]
                raise RuntimeError(
                    f"Embedding batch {batch_number} failed; character_lengths={lengths}: {exception}"
                ) from exception
            self._validate(batch_vectors, expected=len(batch))
            vectors.extend(batch_vectors)
            if on_batch is not None:
                on_batch((batch_number - 1) * self.batch_size, batch_vectors)
        return vectors

    def embed_query(self, query: str) -> list[float]:
        if not query.strip():
            raise ValueError("Query must not be empty")
        started = time.perf_counter()
        try:
            vector = self.embed_documents([query])[0]
        except Exception as exception:
            from devcontext.agentic.models import error_detail

            _record_embedding(self, query, started, success=False,
                              error=error_detail(exception))
            raise
        _record_embedding(self, query, started)
        return vector

    @property
    def effective_transport(self) -> str:
        """What actually carried the request, after any fallback."""
        return "curl" if self._curl_fallback else self.transport

    def _validate(self, vectors: Sequence[Sequence[float]], expected: int) -> None:
        if len(vectors) != expected:
            raise RuntimeError(
                f"Embedding API returned {len(vectors)} vectors for {expected} inputs"
            )
        invalid = [len(vector) for vector in vectors if len(vector) != self.dimensions]
        if invalid:
            raise RuntimeError(
                f"Expected {self.dimensions}-dimensional embeddings, got {invalid}"
            )

    def _embed_with_curl(self, texts: Sequence[str]) -> list[list[float]]:
        executable = shutil.which("curl.exe") or shutil.which("curl")
        if executable is None:
            raise RuntimeError(
                "Embedding connection failed and the curl fallback is unavailable"
            )
        config = "\n".join(
            [
                f'url = "{self.base_url}/embeddings"',
                'request = "POST"',
                f'header = "Authorization: Bearer {self.api_key}"',
                'header = "Content-Type: application/json"',
                "silent",
                "show-error",
            ]
        )
        body = json.dumps(
            {
                "model": self.model,
                "input": list(texts),
                "dimensions": self.dimensions,
                "encoding_format": "float",
            },
            ensure_ascii=False,
        )
        request_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", delete=False
            ) as request_file:
                request_file.write(body)
                request_path = request_file.name
            completed = None
            response_body = ""
            status_code = 0
            for attempt in range(4):
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
                        timeout=bounded_timeout(30),
                        check=False,
                    )
                except subprocess.TimeoutExpired as exception:
                    if attempt == 3:
                        raise RuntimeError(
                            "Bailian curl request timed out after 4 attempts"
                        ) from exception
                    time.sleep(bounded_timeout(2**attempt))
                    continue
                marker = "\n__HTTP_STATUS__:"
                if marker in completed.stdout:
                    response_body, raw_status = completed.stdout.rsplit(marker, 1)
                    status_code = int(raw_status.strip())
                else:
                    response_body = completed.stdout
                    status_code = 0
                retryable = (
                    completed.returncode != 0
                    or status_code == 429
                    or status_code >= 500
                    or "already running" in response_body.lower()
                )
                if not retryable or attempt == 3:
                    break
                time.sleep(bounded_timeout(2**attempt))
        finally:
            if request_path is not None:
                try:
                    os.unlink(request_path)
                except FileNotFoundError:
                    pass
        assert completed is not None
        if completed.returncode != 0 or not 200 <= status_code < 300:
            message = " | ".join(
                item for item in (completed.stderr.strip(), response_body.strip()) if item
            )
            raise RuntimeError(
                f"Bailian curl request failed (HTTP {status_code}): {message}"
            )
        payload = json.loads(response_body)
        data = sorted(payload.get("data", []), key=lambda item: item["index"])
        return [item["embedding"] for item in data]
