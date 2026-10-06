"""Four real requests, no warmup, repeat sampling or quality judge.

Run from the repository root using its Python environment. Existing samples
are never overwritten; --output may select a fresh experiment directory.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

import psycopg

from devcontext.config import Settings


RUNS = (
    ("q1", "multi_pass", "详细解释当前购票占座的数据一致性是如何保证的"),
    ("q1", "single_stream", "详细解释当前购票占座的数据一致性是如何保证的"),
    ("q3", "single_stream", "详细解释当前订单和支付链路是如何保证幂等的"),
    ("q3", "multi_pass", "详细解释当前订单和支付链路是如何保证幂等的"),
)


def index_snapshot(settings):
    with psycopg.connect(settings.database_url, connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT source_type, count(*), md5(string_agg(id::text || ':' || content_hash, ',' ORDER BY id)) "
            "FROM knowledge_chunk WHERE repository=%s GROUP BY source_type ORDER BY source_type",
            (settings.repository_name,),
        ).fetchall()
    return [{"source_type": row[0], "chunks": row[1], "fingerprint": row[2]} for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/single-stream-experiment"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Experiment directory is not empty; samples will not be overwritten or repeated")
    settings = Settings()
    settings.deepseek_key()
    settings.api_key()
    snapshot = index_snapshot(settings)
    # Explicit whitelist: no API keys, credentials or database URLs are saved.
    config_keys = ["deepseek_base_url", "deepseek_model", "deepseek_planner_model",
                   "deepseek_answer_planner_model", "deepseek_answer_model", "deepseek_reviewer_model",
                   "deepseek_context_window", "deepseek_max_output_tokens", "embedding_model",
                   "embedding_dimensions", "embedding_transport", "repository_name", "source_policy_path"]
    config = {key: str(value) if isinstance(value := getattr(settings, key), Path) else value for key in config_keys}
    manifest = {"timestamp": datetime.now(timezone.utc).isoformat(),
                "baseline_commit": "63c6380",
                "implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "config": config, "index_before": snapshot,
                "answer_mode": "teach", "depth": "detailed", "top_k": 12,
                "teaching_reasoning_effort": "high", "samples_per_question_per_mode": 1,
                "quality_ab": False, "runs": []}
    output.mkdir(parents=True)
    manifest_path = output / "manifest.json"
    save = lambda: manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    environment = dict(os.environ, PYTHONIOENCODING="utf-8")
    for question_id, mode, question in RUNS:
        name = f"{question_id}-{mode}"
        command = [sys.executable, "-m", "devcontext.cli", "ask", question,
                   "--answer-mode", "teach", "--depth", "detailed", "--top-k", "12",
                   "--teaching-generation-mode", mode, "--debug", "--perf",
                   "--perf-json", str(output / f"{name}.json")]
        print(f"Starting {name}", flush=True)
        with (output / f"{name}.out").open("wb") as stdout, (output / f"{name}.err").open("wb") as stderr:
            process = subprocess.run(command, stdout=stdout, stderr=stderr, env=environment, check=False)
        text = (output / f"{name}.out").read_text(encoding="utf-8")
        answer = text.split("\nAnswer:\n", 1)[-1].split("\nExplanation Strategy:", 1)[0].strip() if "\nAnswer:\n" in text else ""
        (output / f"{name}.answer.md").write_text(answer + "\n", encoding="utf-8")
        sample = {"name": name, "question": question, "mode": mode, "exit_code": process.returncode}
        perf_path = output / f"{name}.json"
        if perf_path.exists():
            report = json.loads(perf_path.read_text(encoding="utf-8"))
            sample["total_latency_ms"] = report["total_latency_ms"]
            sample["completion_status"] = (report.get("stream") or {}).get("completion_status", "legacy_result")
            print(f"Finished {name}: {sample['total_latency_ms'] / 1000:.1f}s; {sample['completion_status']}", flush=True)
        else:
            print(f"Finished {name}: exit {process.returncode}, no performance report", flush=True)
        manifest["runs"].append(sample)
        save()
    manifest["index_after"] = index_snapshot(settings)
    manifest["index_unchanged"] = manifest["index_after"] == snapshot
    save()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
