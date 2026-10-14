"""Opt-in, request-local diagnostic snapshots; never a production dependency.

The snapshot is a versioned JSON boundary, not a pickle or a second retrieval
engine. Replay restores already retrieved evidence and calls an injected writer.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
from typing import Any

_active = ContextVar("devcontext_diagnostics", default=None)
SCHEMA_VERSION = 1
MILESTONES = ("retrieval_started", "retrieval_finished", "writer_started",
              "provider_first_content", "provider_first_body", "first_section_ready",
              "first_published", "first_body_published", "browser_first_visible", "request_finished")


def clean(value, secrets=()):
    """Scrub nested data, including model/provider errors, before persisting it."""
    if isinstance(value, str):
        for secret in sorted((s for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "[redacted]")
        value = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s\"'<>]+", r"\1[redacted]", value)
        return re.sub(r"(?:postgres(?:ql)?|mysql)://[^\s\"'<>]+", "[redacted connection]", value)
    if isinstance(value, dict):
        return {k: clean(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, secrets) for v in value]
    return value


@dataclass
class DiagnosticCapture:
    secrets: tuple[str, ...] = ()
    clock: Any = time.perf_counter
    started: float = field(init=False)
    timeline: dict = field(default_factory=lambda: dict.fromkeys(MILESTONES))
    events: list = field(default_factory=list)
    retrieval: dict | None = None
    retrieval_progress: dict | None = None
    attempts: list = field(default_factory=list)
    outcome: dict | None = None
    _buffers: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.started = self.clock()

    def mark(self, name, data=None):
        offset = round((self.clock() - self.started) * 1000, 3)
        self.events.append({"type": name, "offset_ms": offset, "data": clean(data, self.secrets)})
        if name in self.timeline and self.timeline[name] is None:
            self.timeline[name] = offset

    def begin(self, client, messages, streaming, stage):
        writer = stage in {"teaching_draft", "teaching_fallback_draft"}
        if writer:
            self.mark("writer_started")
        attempt = {"id": len(self.attempts) + 1, "stage": stage, "writer": writer,
                   "streaming": streaming, "started_ms": round((self.clock()-self.started)*1000, 3),
                   "messages": clean([m.to_dict() for m in messages], self.secrets),
                   "parameters": {k: getattr(client, k, None) for k in
                       ("provider", "model", "max_tokens", "reasoning_effort", "requested_reasoning_effort",
                        "reasoning_omission_reason", "json_mode", "timeout_seconds", "output_token_parameter")},
                   "finished_ms": None, "finish_reason": None, "usage": {}, "error": None}
        self.attempts.append(attempt)
        return attempt["id"]

    def content(self, attempt_id, text):
        attempt = self.attempts[attempt_id - 1]
        if not attempt["writer"]:
            return
        if text.strip():
            self.mark_once("provider_first_content")
        buffer = self._buffers.get(attempt_id, "") + text
        self._buffers[attempt_id] = buffer
        if buffer.lstrip().startswith("<") and ">>>" not in buffer:
            return
        # A protocol marker or Markdown heading is not an explanatory body.
        body = re.sub(r"<<<[^>]*(?:>>>|$)", "", buffer)
        body = re.sub(r"(?m)^\s*#{1,6}[^\n]*(?:\n|$)", "", body)
        if body.strip():
            self.mark_once("provider_first_body")

    def mark_once(self, name):
        if self.timeline[name] is None:
            self.mark(name)

    def end(self, attempt_id, client, error=None):
        self.attempts[attempt_id - 1].update(
            finished_ms=round((self.clock()-self.started)*1000, 3),
            finish_reason=getattr(client, "last_finish_reason", None),
            usage=dict(getattr(client, "last_usage", {})), error=clean(error, self.secrets))

    def finish(self, result=None, error=None):
        teaching = (getattr(getattr(result, "trace", None), "teaching", None) or {})
        self.outcome = clean({
            "generation_status": teaching.get("completion_status", "failed" if error else "unknown"),
            "evidence_status": getattr(getattr(result, "trace", None), "evidence_package_state", None),
            "answer": getattr(getattr(result, "answer_result", None), "answer", ""),
            "used_citations": getattr(getattr(result, "answer_result", None), "used_citations", []),
            "error": error or teaching.get("error"),
            "final_context": (result.context_bundle.to_dict()
                              if getattr(result, "context_bundle", None) is not None else None),
        }, self.secrets)
        self.mark("request_finished")

    def to_dict(self):
        return clean({"schema_version": SCHEMA_VERSION, "timeline": self.timeline,
                      "events": self.events, "retrieval": self.retrieval,
                      "retrieval_progress": self.retrieval_progress,
                      "model_attempts": self.attempts, "outcome": self.outcome}, self.secrets)

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def capture_diagnostics(*, secrets=(), clock=time.perf_counter):
    capture = DiagnosticCapture(tuple(secrets), clock)
    token = _active.set(capture)
    try:
        yield capture
    except BaseException as exc:
        capture.finish(error=f"{type(exc).__name__}: {exc}")
        if isinstance(exc, (KeyboardInterrupt, GeneratorExit)) or type(exc).__name__ == "RequestCancelled":
            capture.outcome["generation_status"] = "cancelled"
        raise
    finally:
        _active.reset(token)


def mark(name, data=None):
    capture = _active.get()
    if capture is not None:
        capture.mark(name, data)


def record_package(request, package):
    capture = _active.get()
    if capture is not None:
        capture.retrieval = clean(snapshot_package(request, package), capture.secrets)
        capture.mark("retrieval_finished", {"state": package.retrieval_state})


def record_progress(plan, workspace, coverage):
    """Keep evidence even if the controller exits before building a package."""
    capture = _active.get()
    if capture is not None:
        capture.retrieval_progress = clean({"evidence_plan": plan.to_dict(),
            "refs": [r.to_dict(include_content=True) for r in workspace.all()],
            "structure": workspace.structural_metadata(),
            "coverage": [c.to_dict() for c in coverage]}, capture.secrets)


def begin_model(client, messages, streaming=False):
    capture = _active.get()
    if capture is None:
        return None
    from devcontext.observability.recorder import current_stage
    return capture.begin(client, messages, streaming, current_stage()[0])


def model_content(attempt_id, text):
    capture = _active.get()
    if capture is not None and attempt_id is not None:
        capture.content(attempt_id, text)


def end_model(attempt_id, client, error=None):
    capture = _active.get()
    if capture is not None and attempt_id is not None:
        capture.end(attempt_id, client, error)


def snapshot_package(request, package):
    workspace = package.evidence_workspace
    data = {"request": request.to_dict(), "package": package.to_dict(),
            "context_bundle": package.context_bundle.to_dict(), "workspace": None,
            "catalog": package.evidence_catalog.to_dict() if package.evidence_catalog is not None else None}
    if workspace is not None:
        data["workspace"] = {
            "refs": [ref.to_dict(include_content=True) for ref in workspace.all()],
            "structure": workspace.structural_metadata(),
            "chunk_ownership_round": [[rid, cid, rnd] for (rid, cid), rnd in workspace._chunk_ownership_round.items()],
            "added_per_round": workspace._added_per_round,
        }
    return deepcopy(data)


def restore_package(snapshot):
    """Restore the v1 JSON contract, preserving recorded identities and rounds."""
    from devcontext.agentic.evidence_models import EvidencePackage, RequirementCoverage, SearchAction, CoverageRound
    from devcontext.answer_policy import RequestPolicy
    from devcontext.context.workspace import EvidenceWorkspace, EvidenceRef
    from devcontext.context.registry import EvidenceCatalog
    from devcontext.context.relations import EvidenceSymbol, EvidenceRelation, RelationProvenance, RelationProbe, ConfirmedPath
    from devcontext.models import Citation, ContextItem, ContextBundle
    from devcontext.planning import EvidencePlan, EvidenceRequirement
    from devcontext.planning.retrieval_need import read_need
    from devcontext.request import UserRequest, AnswerOptions
    data = deepcopy(snapshot)
    raw_request, raw = data["request"], data["package"]
    policy = raw_request.get("policy")
    if policy:
        policy["secondary_intents"] = tuple(policy["secondary_intents"])
        policy = RequestPolicy(**policy)
    request = UserRequest(raw_request["original_query"], AnswerOptions(**raw_request["answer_options"]),
                          raw_request.get("context_budget_override"), policy)
    plan = raw["evidence_plan"]
    requirements = tuple(EvidenceRequirement(**{**r, "retrieval_needs": tuple(read_need(n) for n in r["retrieval_needs"])}) for r in plan["requirements"])
    plan = EvidencePlan(plan["original_query"], requirements, plan["decision_source"], plan["schema_version"])
    context = data["context_bundle"]
    context["items"] = [ContextItem(**{**i, "citation": Citation(**i["citation"])}) for i in context["items"]]
    bundle = ContextBundle(**context)
    def coverage(c):
        return RequirementCoverage(**{**c, "evidence_ids": tuple(c["evidence_ids"]), "missing_criteria": tuple(c["missing_criteria"])})
    workspace = catalog = None
    if data.get("catalog") is not None:
        catalog = EvidenceCatalog(
            {label: Citation(**c) for label, c in data["catalog"]["by_evidence_id"].items()},
            {int(cid): label for cid, label in data["catalog"]["by_chunk_id"].items()})
    if data["workspace"] is not None:
        stored = data["workspace"]
        workspace = EvidenceWorkspace(raw["original_query"])
        for r in stored["refs"]:
            ref = EvidenceRef(**{**r, "citation": Citation(**r["citation"]), "requirement_ids": tuple(r["requirement_ids"])})
            if ref.chunk_id in workspace._refs or ref.evidence_id in workspace.registry._citations:
                raise ValueError("Duplicate evidence identity in snapshot")
            if ref.evidence_id != ref.citation.label or not re.fullmatch(r"E[1-9]\d*", ref.evidence_id):
                raise ValueError("Invalid stable evidence label")
            workspace._refs[ref.chunk_id] = ref
            workspace._order.append(ref.chunk_id)
            workspace.registry._citations[ref.evidence_id] = ref.citation
            workspace.registry._by_chunk_id[ref.chunk_id] = ref.evidence_id
            for rid in ref.requirement_ids:
                workspace._by_requirement.setdefault(rid, []).append(ref.chunk_id)
        workspace.registry._counter = max((int(r.evidence_id[1:]) for r in workspace.all()), default=0)
        workspace._chunk_ownership_round = {(rid, cid): rnd for rid, cid, rnd in stored["chunk_ownership_round"]}
        workspace._added_per_round = {int(k): v for k, v in stored["added_per_round"].items()}
        structure = stored["structure"]
        symbols = {s["symbol_key"]: EvidenceSymbol(**s) for s in structure["symbols"]}
        for o in structure["symbol_ownership"]:
            workspace.add_symbols((symbols[o["symbol_key"]],), o["requirement_id"], o["round_index"])
        relations = {}
        for r in structure["relations"]:
            relation = EvidenceRelation(**r)
            relations[relation.identity] = relation
        # Restore through validation before retaining exact provenance metadata.
        for o in structure["ownership"]:
            workspace.add_relations((relations[tuple(o["relation_id"])],), o["requirement_id"], o["first_seen_round"], "snapshot")
        workspace._relation_provenance = {RelationProvenance(**{**p, "relation_id": tuple(p["relation_id"])}) for p in structure["provenance"]}
        for p in structure["probes"]:
            workspace.add_probe(RelationProbe(**{**p, **{k: tuple(p[k]) for k in ("need_indices", "symbol_keys", "edge_types", "directions")}, "relation_ids": tuple(tuple(r) for r in p["relation_ids"])}))
        for p in structure["paths"]:
            workspace.add_path(ConfirmedPath(**{**p, "nodes": tuple(p["nodes"]), "directions": tuple(p["directions"]), "relation_ids": tuple(tuple(r) for r in p["relation_ids"])}))
        catalog = workspace.freeze()
    package = EvidencePackage(raw["original_query"], plan, bundle,
        tuple(coverage(c) for c in raw["requirement_coverage"]), tuple(raw["unresolved_requirements"]), raw["retrieval_state"],
        tuple(SearchAction(**a) for a in raw["search_history"]),
        tuple(CoverageRound(r["round_index"], tuple(coverage(c) for c in r["statuses"])) for r in raw["coverage_rounds"]), catalog, workspace)
    return request, package


def replay_answer(path, answer_workflow, *, on_section=None):
    """No retrieval or model selection: the caller supplies the answer workflow."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION or data.get("retrieval") is None:
        raise ValueError("Snapshot has no replayable retrieval or unsupported version")
    request, package = restore_package(data["retrieval"])
    return answer_workflow.run(request, package, on_section=on_section)
