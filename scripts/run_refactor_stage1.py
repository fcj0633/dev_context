"""Six single-shot Fast diagnostics; no warmup, judge, reindex or automatic rerun.

Run with the project Python from its root. Raw evidence stays in ignored artifacts.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

import psycopg

from devcontext.application import AskRequest, AskService
from devcontext.config import Settings
from devcontext.observability import capture
from devcontext.observability.diagnostics import capture_diagnostics, clean
from devcontext.observability.report import build_perf_report

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FIELDS = ("llm_provider", "deepseek_model", "deepseek_planner_model", "deepseek_answer_model",
                 "deepseek_answer_planner_model", "openai_model", "embedding_model", "embedding_dimensions",
                 "tool_agent_enabled", "symbol_graph_enabled", "llm_supported_reasoning_efforts",
                 "repository_name", "answer_profile", "answer_reasoning_effort", "teaching_generation_mode")


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fingerprint(settings):
    with psycopg.connect(settings.database_url, connect_timeout=5) as connection:
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            rows = connection.execute(
                "SELECT source_type,count(*),md5(string_agg(id::text||':'||content_hash,',' ORDER BY id)) "
                "FROM knowledge_chunk WHERE repository=%s GROUP BY source_type ORDER BY source_type",
                (settings.repository_name,),
            ).fetchall()
    if not rows:
        raise ValueError("Repository has no indexed evidence")
    return [{"source_type": kind, "chunks": count, "fingerprint": digest} for kind, count, digest in rows]


def blocked(error, consecutive_failures):
    text = (error or "").lower()
    if any(s in text for s in ("http 401", "http 402", "http 403", "insufficient balance", "authentication", "not configured")):
        return True
    return consecutive_failures >= 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "artifacts").resolve()):
        raise SystemExit("Raw diagnostics must be stored inside ignored artifacts/")
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Output already contains samples; will not overwrite or repeat")
    output.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    secrets = tuple(v.get_secret_value() for v in vars(settings).values() if hasattr(v, "get_secret_value")) + (settings.database_url,)
    cases_path = ROOT / "benchmark/refactor-stage1.jsonl"
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    manifest = {"schema_version": 1, "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "tracked_diff_sha256": sha256(subprocess.check_output(["git", "diff", "HEAD"], cwd=ROOT)).hexdigest(),
        "benchmark_sha256": sha256(cases_path.read_bytes()).hexdigest(),
        "config": {k: getattr(settings, k) for k in CONFIG_FIELDS},
        "profile": "fast", "hard_timeout_seconds": 120, "samples_per_case": 1,
        "warmup": False, "judge_calls": 0, "browser_measured": False,
        "reference_manifest": "artifacts/refactor-stage1/references/manifest.json",
        "runs": [], "status": "running"}
    reference_path = ROOT / "docs/refactor/01-reference-sources.json"
    manifest["references"] = json.loads(reference_path.read_text(encoding="utf-8")) if reference_path.exists() else []
    manifest["implementation_files"] = {str(p.relative_to(ROOT)).replace("\\", "/"): sha256(p.read_bytes()).hexdigest()
        for p in [ROOT / "src/devcontext/observability/diagnostics.py", Path(__file__).resolve()]}
    write = lambda: save(output / "manifest.json", clean(manifest, secrets))
    write()
    try:
        manifest["index_before"] = fingerprint(settings)
        settings.text_key()
        settings.api_key()
    except Exception as exc:
        manifest.update(status="external_blocked", error=clean(str(exc), secrets))
        manifest["runs"] = [{"id": c["id"], "status": "not_executed", "reason": "preflight blocked"} for c in cases]
        write()
        print("Preflight blocked; no model request was sent.", flush=True)
        return 2
    write()
    consecutive_failures = 0
    for index, case in enumerate(cases):
        print(f"Starting {case['id']} ({index+1}/{len(cases)}), Fast, 120s limit", flush=True)
        service = AskService(settings)
        prepared = service.prepare(AskRequest(case["question"], profile="fast", hard_timeout=120))
        result = None
        error = None
        started = time.perf_counter()
        with capture() as perf, capture_diagnostics(secrets=secrets) as diagnostic:
            try:
                def published(section):
                    # A completed callback is the measured consumer; not browser paint.
                    print(f"  {case['id']} section {section.section_id} published", flush=True)
                result = service.execute(prepared, on_section=published)
            except Exception as exc:
                error = str(exc)
            finally:
                diagnostic.finish(result, error)
                diagnostic.save(output / f"{case['id']}.snapshot.json")
        elapsed = (time.perf_counter() - started) * 1000
        outcome = diagnostic.to_dict()["outcome"]
        record = {"id": case["id"], "question": case["question"], "elapsed_ms": round(elapsed, 3),
                  "status": outcome["generation_status"], "evidence_status": outcome["evidence_status"],
                  "timeline": diagnostic.timeline, "answer_chars": len(outcome["answer"]),
                  "error": outcome["error"], "snapshot": f"{case['id']}.snapshot.json"}
        if result is not None:
            report = build_perf_report(recorder=perf, result=result, query=case["question"], answer_mode="teach",
                depth=None, top_k=12, wall_clock_ms=elapsed, sample_kind="single_observation",
                timestamp=manifest["timestamp"])
            save(output / f"{case['id']}.perf.json", clean(report, secrets))
        else:
            save(output / f"{case['id']}.perf.json", clean(perf.to_dict(), secrets))
        (output / f"{case['id']}.answer.md").write_text(outcome["answer"], encoding="utf-8")
        save(output / f"{case['id']}.review.json", {"case": case, "status": "pending_manual_review",
            "core_coverage": None, "redundancy": None, "project_claim_support": None,
            "unknown_boundaries": None, "notes": []})
        manifest["runs"].append(record)
        write()
        print(f"Finished {case['id']}: {record['status']}, {elapsed/1000:.1f}s, {record['answer_chars']} chars", flush=True)
        provider_errors = [a["error"] for a in diagnostic.attempts if a["error"]]
        failure = " ".join([str(record["error"] or ""), *provider_errors])
        unavailable = any(s in failure.lower() for s in ("http 429", "http 500", "http 502", "http 503", "connection", "unavailable"))
        consecutive_failures = consecutive_failures + 1 if unavailable else 0
        if blocked(failure, consecutive_failures):
            manifest.update(status="external_blocked", error=failure)
            manifest["runs"].extend({"id": c["id"], "status": "not_executed", "reason": "provider blocked"} for c in cases[index+1:])
            break
    else:
        manifest["status"] = "sampling_finished"
    try:
        manifest["index_after"] = fingerprint(settings)
        manifest["index_unchanged"] = manifest["index_before"] == manifest["index_after"]
    except Exception as exc:
        manifest.update(index_unchanged=None, index_after_error=clean(str(exc), secrets))
    write()
    return 0 if manifest["status"] == "sampling_finished" else 2


if __name__ == "__main__":
    raise SystemExit(main())
