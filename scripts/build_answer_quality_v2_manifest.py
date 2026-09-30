"""Build the Phase 0 frozen baseline manifest. Run from the repo root."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import subprocess
import sys

import psycopg

sys.stdout.reconfigure(encoding="utf-8")

DSN = "postgresql://devcontext:devcontext_dev@localhost:5432/devcontext"
BASE = "artifacts/answer-quality-v2-baseline"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


with psycopg.connect(DSN) as conn, conn.cursor() as cur:
    cur.execute(
        "SELECT source_type, chunk_type, count(*) FROM knowledge_chunk "
        "GROUP BY 1,2 ORDER BY 1,2"
    )
    chunks = {f"{source}/{kind}": count for source, kind, count in cur.fetchall()}
    cur.execute("SELECT count(*), count(embedding) FROM knowledge_chunk")
    total, embedded = cur.fetchone()
    cur.execute("SELECT count(*) FROM knowledge_chunk WHERE file_path LIKE '%.lua'")
    lua = cur.fetchone()[0]

raw = open(f"{BASE}/token-bucket.explain.txt", encoding="utf-8").read()
trace = json.loads(raw[raw.index("Trace:") + len("Trace:"):])
body = raw[raw.index("Answer:"):raw.index("Sources:")]
used = re.findall(r"\[(C\d+)\]", body)
stages = trace["stage_usage"]
llm = [s for s in stages if s.get("model")]
review = trace.get("review") or {}

l1 = json.load(open(f"{BASE}/l1-baseline.json", encoding="utf-8"))
l15 = json.load(open(f"{BASE}/l15-regression-frozen.json", encoding="utf-8"))
l15s = l15["summary"]["frozen"]
tb = next(r for r in l15["records"] if r["case_id"] == "teach-token-bucket-01")

manifest = {
    "schema_version": 1,
    "name": "answer-quality-v2-baseline",
    "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "git_commit": subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip(),
    "git_dirty": bool(
        subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
    ),
    "why": (
        "Phase 0 freeze before Answer Quality V3. The token-bucket answer below is the "
        "verbatim baseline the teaching path must be compared against."
    ),
    "environment": {
        "no_env_file": True,
        "api_keys_from": "Windows environment (DEEPSEEK_API_KEY, DASHSCOPE_API_KEY)",
        "no_proxy_required": "localhost,127.0.0.1,::1,.local,.aliyuncs.com",
        "no_proxy_reason": (
            "The machine proxy breaks the TLS handshake to dashscope.aliyuncs.com; without "
            "the bypass every embedding call fails and retrieval silently returns empty."
        ),
        "postgres": "docker container devcontext-postgres (pgvector 0.8.6 / pg18) on 127.0.0.1:5432",
    },
    "corpus": {
        "repository": "my12306",
        "total_chunks": total,
        "with_embedding": embedded,
        "by_kind": chunks,
        "lua_chunks": lua,
        "lua_note": (
            "Lua scripts are not ingested, so take_token_from_bucket.lua / "
            "return_token_to_bucket.lua can never appear as evidence."
        ),
    },
    "l1": {
        "benchmark_sha256": l1["benchmark_sha256"],
        "baseline_comparison": l1["baseline_comparison"]["status"],
        "quality_passed": l1["quality_passed"],
        "policy_improvement_passed": l1["policy_improvement_passed"],
        "acceptance": l1["acceptance"],
        "noise_floor_note": (
            "MRR deltas of up to ~0.015 against the committed baseline are run-to-run noise; "
            "L1 is not bit-reproducible."
        ),
    },
    "l15_frozen": {
        "suite": "l1.5+regression",
        "case_count": l15s["case_run_count"],
        "full_case_success_rate": l15s["full_case_success_rate"],
        "core_requirement_coverage": l15s["core_requirement_coverage"],
        "expected_state_accuracy": l15s["expected_state_accuracy"],
        "context_survival_rate": l15s["context_survival_rate"],
        "false_ready_count": l15s["false_ready_count"],
        "regression_vs_retrieval_workflow_v1": (
            "the 23 pre-existing cases reproduce the committed baseline exactly "
            "(READY 16/18 = 0.8889 all_core_satisfied, READY state_accuracy 0.8333, "
            "PARTIAL acc 1.0, EMPTY acc 0.0)"
        ),
        "new_case": {
            "id": "teach-token-bucket-01",
            "actual_state": tb["actual_retrieval_state"],
            "full_case_success": tb["full_case_success"],
            "core_requirement_coverage": tb["core_requirement_coverage"],
            "false_ready": tb["false_ready"],
        },
    },
    "token_bucket_explain_baseline": {
        "raw_file": f"{BASE}/token-bucket.explain.txt",
        "command": (
            'devcontext ask "详细解释项目的余票桶是如何设计的" '
            "--answer-mode explain --depth detailed --debug"
        ),
        "answer_sha256": sha256_text(body),
        "answer_chars": len(body),
        "section_count": len(re.findall(r"(?m)^## ", body)),
        "used_citations": sorted(set(used)),
        "reported_sufficiency": "insufficient",
        "retry_count": trace["retry_count"],
        "retrieval_state": trace["evidence_package_state"],
        "search_action_errors": sum(1 for a in trace["search_actions"] if a.get("error")),
        "coverage": [
            {
                "requirement_id": c["requirement_id"],
                "state": c["state"],
                "check_error": c.get("check_error"),
                "reason": c["reason"],
            }
            for c in trace["final_coverage"]
        ],
        "review": {
            "accepted": review.get("accepted"),
            "issue_types": [i["issue_type"] for i in (review.get("issues") or [])],
            "decision_source": review.get("decision_source"),
        },
        "stage_usage": [
            {
                "stage": s["stage"],
                "latency_ms": round(float(s["latency_ms"])),
                "model": s.get("model"),
                "input_tokens": s.get("input_tokens"),
                "output_tokens": s.get("output_tokens"),
            }
            for s in stages
        ],
        "totals": {
            "llm_calls": len(llm),
            "input_tokens": sum(int(s.get("input_tokens") or 0) for s in llm),
            "output_tokens": sum(int(s.get("output_tokens") or 0) for s in llm),
            "latency_ms": round(sum(float(s["latency_ms"]) for s in stages)),
        },
    },
    "known_defects_visible_in_this_baseline": [
        "CoverageChecker: the semantic check LLM call succeeds (11288 in / 3337 out) but its "
        "output is rejected by the strict parser, so all six requirements become UNVERIFIED "
        "with check_error=CoverageCheckError. That is reported to the user as 'insufficient' "
        "and, per requirement, 'missing' - a parse failure presented as missing evidence.",
        "CLI _print_requirement_statuses collapses PARTIAL/MISSING/UNVERIFIED into one word, "
        "'missing'.",
        "retrieval_state EMPTY cannot distinguish 'nothing found' from 'retrieval failed'. "
        "Demonstrated by the preserved proxy-failure run, where all ten search actions errored "
        "and the tool reported that insufficient project context had been retrieved.",
    ],
    "reviewer_working_as_intended": [
        "The reviewer rejected the grounded draft and removed the unsupported 'Lua 原子取还' "
        "claims (the final answer contains zero occurrences of the character pair), precisely "
        "because the Lua script bodies are not in the index. Any change to reviewer gating must "
        "not lose this behaviour.",
    ],
}

path = "benchmark/baselines/answer-quality-v2-manifest.json"
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(manifest, handle, ensure_ascii=False, indent=2)
    handle.write("\n")

print("wrote", path)
print("git_commit:", manifest["git_commit"][:12], "| dirty:", manifest["git_dirty"])
print("corpus:", total, "chunks,", embedded, "embedded, lua =", lua)
print(
    "tb answer sha256:",
    manifest["token_bucket_explain_baseline"]["answer_sha256"][:16],
    "| chars",
    len(body),
    "| sections",
    manifest["token_bucket_explain_baseline"]["section_count"],
)
print("tb totals:", manifest["token_bucket_explain_baseline"]["totals"])
