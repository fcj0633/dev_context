"""Single-shot network and RAG comparisons, with isolated child environments.

Does not change the VPN, credentials, product settings, or knowledge index.
Raw answers and diagnostic requests stay under ignored artifacts/.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from devcontext.application import AskRequest, AskService
from devcontext.config import Settings, project_root
from devcontext.observability.diagnostics import capture_diagnostics, clean
from devcontext.observability.recorder import capture

QUESTION = "用户注册入口位于哪个类？请说明接口路径，以及它调用的服务方法。"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def child(args):
    # Reproduce the original inherited transport even after local explicit routes
    # are enabled. The direct child isolates proxies through NO_PROXY=*.
    settings = Settings().model_copy(update={"llm_provider": args.provider,
        "openai_proxy_url": None, "embedding_bypass_proxy": False, "deepseek_bypass_proxy": False})
    secrets = tuple(v.get_secret_value() for v in vars(settings).values()
                    if hasattr(v, "get_secret_value")) + (settings.database_url,)
    started = time.perf_counter()
    first_delta = None
    result = None
    error = None
    def event(value):
        nonlocal first_delta
        if value.type == "answer.text_delta" and first_delta is None:
            first_delta = (time.perf_counter() - started) * 1000
    with capture() as perf, capture_diagnostics(secrets=secrets) as diagnostic:
        try:
            result = AskService(settings).ask(AskRequest(QUESTION, profile="fast", hard_timeout=240), on_event=event)
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        finally:
            diagnostic.finish(result, error)
            diagnostic.save(args.output / (args.sample + ".snapshot.json"))
    answer = getattr(getattr(result, "answer_result", None), "answer", "") or ""
    teaching = getattr(getattr(result, "trace", None), "teaching", None) or {}
    summary = {"sample": args.sample, "provider": args.provider, "query": QUESTION,
        "model": settings.text_model(), "elapsed_ms": (time.perf_counter()-started)*1000,
        "first_delta_ms": first_delta, "timeline": diagnostic.timeline,
        "status": teaching.get("completion_status", "failed" if error else "unknown"),
        "evidence_state": getattr(getattr(result, "trace", None), "evidence_package_state", None),
        "answer_chars": len(answer), "error": error, "perf": perf.to_dict(),
        "attempts": [{k: a[k] for k in ("id", "stage", "started_ms", "finished_ms", "usage", "error", "parameters")}
                     for a in diagnostic.attempts]}
    save(args.output / (args.sample + ".summary.json"), clean(summary, secrets))
    (args.output / (args.sample + ".answer.md")).write_text(clean(answer, secrets), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("sample", "status", "elapsed_ms", "answer_chars", "first_delta_ms")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--proxy", default="http://127.0.0.1:12334")
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--provider", choices=["openai", "deepseek"])
    parser.add_argument("--sample")
    args = parser.parse_args()
    args.output = args.output.resolve()
    if not args.output.is_relative_to((project_root() / "artifacts").resolve()):
        raise SystemExit("output must be under ignored artifacts/")
    if args.child:
        child(args)
        return
    if args.output.exists():
        raise SystemExit("refusing to overwrite or rerun existing experiment")
    args.output.mkdir(parents=True)
    settings = Settings()
    manifest = {"timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "question": QUESTION, "profile": "fast", "synthesis": settings.answer_synthesis_mode,
        "deadline_seconds": 240, "automatic_reruns": False, "runs": [], "connections": []}
    # Unauthenticated GET probes: no generation, no API credentials in config/output.
    for provider, base in [("gpt", settings.openai_base_url), ("deepseek", settings.deepseek_base_url),
                           ("embedding", settings.dashscope_base_url)]:
        endpoint = (base or "").rstrip("/") + ("/embeddings" if provider == "embedding" else "/models")
        for route in ("direct", "proxy"):
            config = '\n'.join([f'url = "{endpoint}"', 'noproxy = "' + ('*' if route == 'direct' else '') + '"',
                'proxy = "' + ('' if route == 'direct' else args.proxy) + '"'])
            proc = subprocess.run(["curl.exe", "--disable", "--config", "-", "--silent", "--output", "NUL",
                "--connect-timeout", "8", "--max-time", "15", "--write-out", "%{json}"], input=config,
                text=True, encoding="utf-8", capture_output=True, timeout=18)
            try:
                measured = json.loads(proc.stdout)
                row = {k: measured.get(k) for k in ("http_code", "time_namelookup", "time_connect", "time_appconnect",
                    "time_starttransfer", "time_total", "remote_ip", "remote_port", "proxy_used")}
            except ValueError:
                row = {"http_code": None}
            row.update(provider=provider, route=route, exit_code=proc.returncode)
            manifest["connections"].append(row)
            save(args.output / "manifest.json", manifest)
            print(json.dumps(row), flush=True)
    # Same query, one run per combination. Alternate provider order between routes.
    # current inherits actual proxy/no_proxy; direct affects only the child process.
    for provider, route in [("deepseek", "current"), ("openai", "current"),
                            ("openai", "direct"), ("deepseek", "direct")]:
        env = dict(os.environ)
        if route == "direct":
            for name in tuple(env):
                if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
                    env.pop(name)
            env["NO_PROXY"] = "*"
        sample = provider + "-" + route
        print(sample + ": starting full RAG request", flush=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--child", "--provider", provider,
                   "--sample", sample, "--output", str(args.output)]
        try:
            proc = subprocess.run(command, env=env, timeout=260, capture_output=True, text=True, encoding="utf-8")
            summary_path = args.output / (sample + ".summary.json")
            if summary_path.exists():
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                manifest["runs"].append({k: summary[k] for k in ("sample", "status", "elapsed_ms", "answer_chars", "first_delta_ms")})
                print(proc.stdout.strip(), flush=True)
            else:
                manifest["runs"].append({"sample": sample, "status": "process_failed", "exit_code": proc.returncode})
                print(sample + ": process failed; no summary", flush=True)
        except subprocess.TimeoutExpired:
            manifest["runs"].append({"sample": sample, "status": "process_timeout"})
        save(args.output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
