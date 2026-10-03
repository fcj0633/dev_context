"""Reuse historical V1; run V2 Q1 and Q3 once, with a free balance preflight."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import psycopg

from devcontext.config import Settings
from devcontext.explanation.style import inspect_answer


BASELINE_COMMIT = "4a3a3f2"
CASES = (
    ("q1", "详细解释当前购票占座的数据一致性是如何保证的"),
    ("q3", "详细解释当前订单和支付链路是如何保证幂等的"),
)


def check_balance(settings):
    executable = shutil.which("curl.exe") or shutil.which("curl")
    if not executable:
        return {"status": "error", "error": "curl unavailable"}
    # Keys stay off command lines, persisted files, and diagnostics. Only the
    # availability flag is saved; account amounts are not needed for this test.
    config = "\n".join([f'url = "{settings.deepseek_base_url.rstrip("/")}/user/balance"',
                        f'header = "Authorization: Bearer {settings.deepseek_key()}"',
                        "silent", "show-error"])
    try:
        completed = subprocess.run([executable, "--config", "-", "--max-time", "20",
                                    "--write-out", "\n__HTTP_STATUS__:%{http_code}"],
                                   input=config, encoding="utf-8", capture_output=True, timeout=25, check=False)
        body, marker, code = completed.stdout.rpartition("\n__HTTP_STATUS__:")
        if not marker or completed.returncode or code.strip() != "200":
            return {"status": "error", "error": f"balance endpoint failed (HTTP {code.strip() or 'unknown'})"}
        available = json.loads(body).get("is_available")
        if type(available) is not bool:
            return {"status": "error", "error": "invalid balance response"}
        return {"status": "available" if available else "unavailable", "is_available": available}
    except Exception as exc:
        return {"status": "error", "error": f"balance check failed: {type(exc).__name__}"}


def index_snapshot(settings):
    with psycopg.connect(settings.database_url, connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT source_type, count(*), md5(string_agg(id::text || ':' || content_hash, ',' ORDER BY id)) "
            "FROM knowledge_chunk WHERE repository=%s GROUP BY source_type ORDER BY source_type",
            (settings.repository_name,),
        ).fetchall()
    return [{"source_type": row[0], "chunks": row[1], "fingerprint": row[2]} for row in rows]


def extract_answer(output):
    if "\nAnswer:\n" not in output:
        return ""
    return output.split("\nAnswer:\n", 1)[1].split("\nExplanation Strategy:", 1)[0].strip()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/teaching-answer-v2"))
    parser.add_argument("--v1-dir", type=Path, default=Path("artifacts/single-stream-experiment"))
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    output, historical = args.output.resolve(), args.v1_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Output is not empty; existing results will not be overwritten or repeated")
    for case_id, _ in CASES:
        if not (historical / f"{case_id}-single_stream.answer.md").exists():
            raise SystemExit(f"Historical V1 answer missing: {case_id}")
    settings = Settings()
    settings.deepseek_key()
    output.mkdir(parents=True)
    manifest = {"timestamp": datetime.now(timezone.utc).isoformat(), "baseline_commit": BASELINE_COMMIT,
                "implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "plan_schema_version": "teaching_v2", "answer_mode": "teach", "depth": "detailed", "top_k": 12,
                "model": settings.deepseek_model, "answer_model": settings.deepseek_answer_model,
                "answer_planner_model": settings.deepseek_answer_planner_model, "reasoning_effort": "high",
                "context_window": settings.deepseek_context_window, "max_output_tokens": settings.deepseek_max_output_tokens,
                "embedding_model": settings.embedding_model, "embedding_dimensions": settings.embedding_dimensions,
                "quality_judge": False, "samples_per_question": 1, "runs": []}
    manifest["v1_source_directory"] = str(historical)
    manifest["config"] = {key: getattr(settings, key, None) for key in (
        "deepseek_base_url", "deepseek_model", "deepseek_planner_model", "deepseek_answer_planner_model",
        "deepseek_answer_model", "deepseek_reviewer_model", "deepseek_context_window", "deepseek_max_output_tokens",
        "embedding_model", "embedding_dimensions", "embedding_transport", "repository_name", "source_policy_path")}
    if (historical / "manifest.json").exists():
        previous = json.loads((historical / "manifest.json").read_text(encoding="utf-8"))
        manifest["v1_sample_commit"] = previous.get("implementation_commit")
    save = lambda: write_json(output / "manifest.json", manifest)
    for case_id, _ in CASES:
        answer = (historical / f"{case_id}-single_stream.answer.md").read_text(encoding="utf-8")
        original = json.loads((historical / f"{case_id}-single_stream.json").read_text(encoding="utf-8"))
        status = original["stream"]["completion_status"]
        (output / f"{case_id}-v1.answer.md").write_text(answer, encoding="utf-8")
        write_json(output / f"{case_id}-v1.readability.json", inspect_answer(answer, status))
    manifest["balance_preflight"] = check_balance(settings)
    save()
    if manifest["balance_preflight"]["status"] != "available":
        manifest["runs"] = [{"case": case_id, "status": "not_run", "reason": "balance preflight unavailable"}
                            for case_id, _ in CASES]
        save()
        print("Balance preflight unavailable; Q1 and Q3 were not run. Historical metrics were saved.", flush=True)
        return 2
    settings.api_key()
    snapshot = index_snapshot(settings)
    manifest["index_before"] = snapshot
    if (historical / "manifest.json").exists():
        manifest["index_matches_v1"] = snapshot == previous.get("index_before")
    save()
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    exhausted = False
    for case_id, question in CASES:
        if exhausted:
            manifest["runs"].append({"case": case_id, "status": "not_run", "reason": "prior request HTTP 402"})
            save()
            continue
        name = f"{case_id}-v2"
        command = [sys.executable, "-m", "devcontext.cli", "ask", question,
                   "--answer-mode", "teach", "--depth", "detailed", "--top-k", "12",
                   "--teaching-generation-mode", "single_stream", "--debug", "--perf",
                   "--perf-json", str(output / f"{name}.json")]
        print(f"Starting {name}", flush=True)
        with (output / f"{name}.out").open("wb") as stdout, (output / f"{name}.err").open("wb") as stderr:
            process = subprocess.run(command, stdout=stdout, stderr=stderr, env=environment, check=False)
        text = (output / f"{name}.out").read_text(encoding="utf-8")
        stderr_text = (output / f"{name}.err").read_text(encoding="utf-8")
        answer = extract_answer(text)
        (output / f"{name}.answer.md").write_text(answer + "\n", encoding="utf-8")
        if "\nTrace:\n" in text:
            trace, _ = json.JSONDecoder().raw_decode(text.split("\nTrace:\n", 1)[1].lstrip())
            write_json(output / f"{name}.trace.json", trace)
        run = {"case": case_id, "exit_code": process.returncode}
        if (output / f"{name}.json").exists():
            report = json.loads((output / f"{name}.json").read_text(encoding="utf-8"))
            stream = report.get("stream") or {}
            run.update(status=stream.get("completion_status", "failed"), total_latency_ms=report["total_latency_ms"],
                       llm_calls=report["summary"]["llm_calls"], sections_planned=stream.get("sections_planned"),
                       sections_emitted=stream.get("sections_emitted"), stream_retry_count=stream.get("stream_retry_count"))
            write_json(output / f"{name}.readability.json", report.get("readability"))
        else:
            run.update(status="failed", error="no performance report")
        exhausted = "HTTP 402" in text + stderr_text
        manifest["runs"].append(run)
        save()
        print(f"Finished {name}: {run['status']}; {run.get('total_latency_ms', 0) / 1000:.1f}s", flush=True)
    manifest["index_after"] = index_snapshot(settings)
    manifest["index_unchanged"] = manifest["index_after"] == snapshot
    save()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
