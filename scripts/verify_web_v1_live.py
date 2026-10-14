"""Explicit live acceptance. Uses real retrieval/model configuration, never a test double."""
from pathlib import Path
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from devcontext.web.api import create_app
from devcontext.config import Settings, project_root


def main():
    settings = Settings()
    report = {"kind": "live", "runs": []}
    app = create_app(settings=settings, database_path=project_root() / "data/web/live-acceptance.sqlite3")
    with TestClient(app) as client:
        for profile in ("fast", "full"):
            sid = client.post("/api/v1/sessions", json={"title": f"V1 live {profile}"}).json()["id"]
            started = time.monotonic()
            response = client.post(f"/api/v1/sessions/{sid}/runs", json={
                "query": "OrderServiceImpl.createTicketOrder 方法在哪个文件，主要负责什么？",
                "profile": profile, "client_request_id": str(uuid4())})
            response.raise_for_status()
            rid = response.json()["id"]
            print(f"{profile}: started {rid}", flush=True)
            while True:
                response = client.get(f"/api/v1/runs/{rid}")
                response.raise_for_status()
                run = response.json()
                if not run["worker_active"]:
                    break
                time.sleep(1)
            events = app.state.manager.store.events(rid, limit=10000)
            labels = {e["label"] for e in run["evidence"]}
            used = {label for s in run["sections"] for label in s["citations"]}
            refreshed = client.get(f"/api/v1/sessions/{sid}").json()["runs"][0]
            replay = client.get(f"/api/v1/runs/{rid}/events").text
            entry = {"profile": profile, "status": run["status"], "reason": run["reason"],
                "elapsed_seconds": round(time.monotonic()-started, 2), "section_count": len(run["sections"]),
                "evidence_count": len(labels), "used_citation_count": len(used), "all_citations_resolved": used <= labels,
                "chapters_match_final": bool(run["sections"]) and run["answer"] == "\n\n".join(s["markdown"] for s in run["sections"]),
                "refresh_matches": refreshed["answer"] == run["answer"], "sse_replay_matches": replay.count("data: ") == len(events),
                "summary": run["summary"], "error": run["error"]}
            report["runs"].append(entry)
            print(json.dumps({k: v for k,v in entry.items() if k != "summary"}, ensure_ascii=False), flush=True)
    report["passed"] = all(r["status"] == "completed" and r["all_citations_resolved"] and r["chapters_match_final"]
                            and r["refresh_matches"] and r["sse_replay_matches"] for r in report["runs"])
    output = project_root() / "artifacts/web-v1-live.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
