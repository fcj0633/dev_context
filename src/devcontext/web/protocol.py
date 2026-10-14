from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
import re

TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})
EVENT_TYPES = frozenset({"run.created", "run.started", "retrieval.action_completed", "retrieval.context_built",
                         "evidence.updated", "answer.section_upsert", "answer.generation_started", "answer.section_started",
                         "answer.text_delta", "answer.generation_finished", "run.summary", "run.finished"})


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class RunEvent:
    run_id: str
    seq: int
    type: str
    timestamp: str
    data: dict[str, Any]

    def to_dict(self):
        return asdict(self)


class RunError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class PublicData:
    """Remove configured secrets even when a model or provider echoes them."""
    def __init__(self, settings):
        self.secrets = [v.get_secret_value() for v in vars(settings).values()
                        if hasattr(v, "get_secret_value")]
        self.secrets.append(settings.database_url)

    def clean(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[已隐藏]")
            return re.sub(r"(?:postgres(?:ql)?|mysql)://[^\s<>\"']+", "[连接配置已隐藏]", value)
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        return value


def evidence_items(bundle, catalog=None):
    items = []
    for item in bundle.items:
        citation = item.citation
        if catalog is not None and (catalog.label_for(item.chunk_id) != citation.label or
                                    catalog.citation(citation.label) != citation):
            continue
        items.append({"label": citation.label, "chunk_id": item.chunk_id, "citation": citation.to_dict(),
                      "content": item.content, "truncated": item.truncated, "chunk_type": item.chunk_type,
                      "source_role": item.source_role, "temporal_status": item.temporal_status})
    return items


def execution_summary(trace):
    teaching = trace.teaching or {}
    agent = trace.agent_retrieval or {}
    # Model diagnostics, rationale, raw responses and arbitrary dictionaries are never copied.
    stages = [{"stage": s.stage, "latency_ms": s.latency_ms, "decision_source": s.decision_source,
               "input_tokens": s.input_tokens, "output_tokens": s.output_tokens, "round_index": s.round_index}
              for s in trace.stage_usage]
    steps = []
    for step in agent.get("steps", []):
        steps.append({"step": step.get("step_number"), "tools": [c.get("tool_name") for c in step.get("planned_calls", [])],
                      "new_evidence_count": len(step.get("new_evidence_ids", [])), "latency_ms": step.get("latency_ms"),
                      "progress": bool(step.get("progress"))})
    return {"stages": stages, "steps": steps,
            "coverage": [{"requirement_id": c.get("requirement_id"), "state": c.get("state")}
                         for c in trace.final_coverage],
            "evidence_state": trace.evidence_package_state, "stop_reason": trace.stop_reason,
            "retry_count": trace.retry_count, "generation_status": teaching.get("completion_status"),
            "delivery_path": teaching.get("delivery_path"), "sections_emitted": teaching.get("sections_emitted", 0),
            "planner_retry_count": teaching.get("planner_retry_count", 0),
            "stream_retry_count": teaching.get("stream_retry_count", 0)}
