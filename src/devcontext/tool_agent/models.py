from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from devcontext.agentic.evidence_models import RequirementCoverage
from devcontext.models import SearchResult

ToolStatus = Literal["SUCCESS", "EMPTY", "PARTIAL", "AMBIGUOUS", "INVALID_ARGUMENT", "ERROR", "DEADLINE"]
StopReason = Literal["READY", "MAX_STEPS", "MAX_TOOL_CALLS", "NO_PROGRESS", "NO_USEFUL_TOOL", "DEADLINE", "PLANNER_FAILED", "ALL_TOOLS_FAILED"]
MAX_STEPS = 3
MAX_TOOL_CALLS = 8
MAX_CALLS_PER_STEP = 3


@dataclass(frozen=True, slots=True)
class SymbolObservation:
    symbol_key: str
    symbol_kind: str
    chunk_id: int
    file_path: str
    state: Literal["CONFIRMED", "CANDIDATE"] = "CONFIRMED"

    @classmethod
    def from_row(cls, row: dict, state="CONFIRMED"):
        return cls(row["symbol_key"], row["symbol_kind"], row["chunk_id"], row["file_path"], state)


@dataclass(frozen=True, slots=True)
class RelationObservation:
    source: str
    target: str
    edge_type: str
    direction: str
    hop: int
    resolution_kind: str
    source_line: int
    source_column: int


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    requirement_id: str
    tool_name: str
    arguments: dict[str, str]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ToolError:
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class ToolObservation:
    discovered_symbols: tuple[SymbolObservation, ...] = ()
    candidates: tuple[SymbolObservation, ...] = ()
    graph_relations: tuple[RelationObservation, ...] = ()
    file_paths: tuple[str, ...] = ()
    truncated: bool = False
    summary: str = ""
    strategy: str | None = None

    def to_dict(self):
        result = asdict(self)
        result["calls"] = [asdict(r) for r in self.graph_relations if r.edge_type == "CALLS"]
        result["constructs"] = [asdict(r) for r in self.graph_relations if r.edge_type == "CONSTRUCTS"]
        return result


@dataclass(frozen=True, slots=True)
class ToolResult:
    call: ToolCall
    status: ToolStatus
    evidence_results: tuple[SearchResult, ...] = ()
    observation: ToolObservation = field(default_factory=ToolObservation)
    latency_ms: float = 0.0
    error: ToolError | None = None
    returned_ids: tuple[int, ...] = ()
    new_evidence_count: int = 0
    new_symbol_count: int = 0
    new_ownership_count: int = 0

    @property
    def completed(self):
        return self.status in {"SUCCESS", "EMPTY", "PARTIAL", "AMBIGUOUS"}

    def to_dict(self):
        return {"call": asdict(self.call), "status": self.status,
                "evidence_ids": [item.id for item in self.evidence_results] or list(self.returned_ids),
                "observation": self.observation.to_dict(), "latency_ms": self.latency_ms,
                "new_evidence_count": self.new_evidence_count, "new_symbol_count": self.new_symbol_count,
                "new_ownership_count": self.new_ownership_count,
                "error": asdict(self.error) if self.error else None}


@dataclass(frozen=True, slots=True)
class AgentDecision:
    calls: tuple[ToolCall, ...]
    cannot_progress: bool = False
    decision_source: str = "llm"


@dataclass(frozen=True, slots=True)
class ActionStep:
    step_number: int
    coverage_before: tuple[RequirementCoverage, ...]
    planned_calls: tuple[ToolCall, ...]
    results: tuple[ToolResult, ...]
    new_evidence_ids: tuple[int, ...]
    new_symbols: tuple[str, ...]
    new_ownerships: tuple[tuple[str, int], ...]
    coverage_after: tuple[RequirementCoverage, ...]
    progress: bool
    latency_ms: float
    new_relation_count: int = 0
    new_relation_ownership_count: int = 0
    coverage_diagnostics: dict = field(default_factory=dict)

    def to_dict(self):
        return {"step_number": self.step_number,
                "coverage_before": [c.to_dict() for c in self.coverage_before],
                "planned_calls": [asdict(c) for c in self.planned_calls],
                "results": [r.to_dict() for r in self.results],
                "new_evidence_ids": list(self.new_evidence_ids), "new_symbols": list(self.new_symbols),
                "new_ownerships": list(self.new_ownerships),
                "coverage_after": [c.to_dict() for c in self.coverage_after],
                "progress": self.progress, "latency_ms": self.latency_ms,
                "new_relation": self.new_relation_count, "new_relation_ownership": self.new_relation_ownership_count,
                "coverage_diagnostics": self.coverage_diagnostics}


@dataclass(frozen=True, slots=True)
class AgentObservationView:
    payload: dict[str, Any]
    confirmed_keys: frozenset[str]

    def keys_for(self, requirement_id):
        return frozenset(s['symbol_key'] for r in self.payload.get('requirements', []) if r['id'] == requirement_id
                         for s in r.get('known_symbols', []) if s['state'] == 'CONFIRMED')
