"""Minimal API probe. Run: uv run python scripts/check_chatgpt_api.py"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from urllib.parse import urlsplit

from openai import APIStatusError, OpenAI


def setting(name: str) -> tuple[str, str]:
    value = os.environ.get(name, "").strip()
    if value:
        return value, "process environment"
    if os.name == "nt":
        import winreg
        # A running terminal may not have inherited newly saved Windows values.
        for root, path, source in (
            (winreg.HKEY_CURRENT_USER, "Environment", "Windows user environment"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
             "Windows system environment"),
        ):
            try:
                with winreg.OpenKey(root, path) as registry:
                    value = str(winreg.QueryValueEx(registry, name)[0]).strip()
                    if value:
                        return value, source
            except OSError:
                pass
    return "", "missing"


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe CHATGPT_API_KEY / OPENAI_BASE_URL without printing the key.")
    parser.add_argument("--model", default="gpt-6.1-sol", help="Model to probe (default: gpt-6.1-sol)")
    parser.add_argument("--api", choices=("auto", "responses", "chat"), default="auto")
    parser.add_argument("--timeout", type=float, default=45, help="Timeout per request in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    api_key, key_source = setting("CHATGPT_API_KEY")
    base_url, url_source = setting("OPENAI_BASE_URL")
    if not api_key or not base_url:
        print("FAIL: missing " + ", ".join(n for n, v in (
            ("CHATGPT_API_KEY", api_key), ("OPENAI_BASE_URL", base_url)) if not v))
        return 1
    base_url = base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        print("FAIL: OPENAI_BASE_URL must be an HTTP(S) API base URL without credentials/query/fragment.")
        return 1
    # Permit a full endpoint pasted instead of the API base.
    for suffix in ("/chat/completions", "/responses", "/models"):
        if base_url.endswith(suffix):
            base_url = base_url[:-len(suffix)]
            break
    if not urlsplit(base_url).path:
        base_url += "/v1"
    print(f"API key: configured ({key_source}); value hidden")
    print(f"Base URL: {base_url} ({url_source})")
    started = time.perf_counter()
    try:
        with OpenAI(api_key=api_key, base_url=base_url, timeout=args.timeout, max_retries=0) as client:
            model = args.model
            print(f"Selected model: {model}")
            endpoint = args.api
            if endpoint in {"auto", "responses"}:
                try:
                    result = client.responses.create(model=model, input="Reply only with OK.",
                                                     max_output_tokens=512, store=False)
                    text = result.output_text
                    endpoint = "responses"
                except APIStatusError as exc:
                    if args.api != "auto" or exc.status_code not in {404, 405, 501}:
                        raise
                    print(f"Responses unavailable (HTTP {exc.status_code}); trying Chat Completions.")
                    endpoint = "chat"
            if endpoint == "chat":
                result = client.chat.completions.create(model=model,
                    messages=[{"role": "user", "content": "Reply only with OK."}], max_completion_tokens=512)
                text = result.choices[0].message.content if result.choices else ""
            if not text or not text.strip():
                print("FAIL: request returned without usable text.")
                return 1
            print(f"SUCCESS: {endpoint}; response: {text.strip()[:200]}")
            print(f"Elapsed: {time.perf_counter() - started:.2f}s")
            if result.usage is not None:
                print("Usage: " + result.usage.model_dump_json(exclude_none=True))
            return 0
    except Exception as exc:
        detail = str(exc).replace(api_key, "[REDACTED]")
        detail = re.sub(r"sk-[A-Za-z0-9_-]+", "[REDACTED]", detail)
        print(f"FAIL: {type(exc).__name__}: {detail[:600]}")
        print(f"Elapsed: {time.perf_counter() - started:.2f}s")
        return 1


if __name__ == "__main__":
    sys.exit(main())
