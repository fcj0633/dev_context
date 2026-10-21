"""Request-local agent state and immutable inputs for decision makers."""
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping

from devcontext.agentic.evidence_models import CoverageRound, RequirementCoverage, SearchAction, ToolExecutionSummary
from devcontext.agentic.models import StageUsage
from devcontext.context.workspace import EvidenceWorkspace, WorkspaceProgressSnapshot
from devcontext.evidence import EvidencePool
from devcontext.planning import EvidencePlan
from devcontext.tool_agent.models import ActionStep, StopReason, SymbolObservation, ToolResult


def _freeze_arguments(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze_arguments(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_arguments(v) for v in value)
    return value


@dataclass(slots=True)
class TaskProgress:
    coverage: tuple[RequirementCoverage, ...] = ()


@dataclass(slots=True)
class AgentWorkingMemory:
    available_symbols: dict[str, SymbolObservation] = field(default_factory=dict)
    symbol_requirements: dict[str, set[str]] = field(default_factory=dict)
    candidates: dict[str, SymbolObservation] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionHistory:
    steps: list[ActionStep] = field(default_factory=list)
    tool_history: list[ToolResult] = field(default_factory=list)
    coverage_rounds: list[CoverageRound] = field(default_factory=list)
    search_history: list[SearchAction] = field(default_factory=list)
    stages: list[StageUsage] = field(default_factory=list)

    def execution_summary(self):
        completed = sum(r.completed for r in self.tool_history)
        return ToolExecutionSummary(len(self.tool_history), completed, len(self.tool_history) - completed)

    @property
    def errors(self):
        return tuple(r.error for r in self.tool_history if r.error is not None)


@dataclass(slots=True)
class RuntimeControl:
    planner_failures: int = 0
    planner_diagnostics: list[dict] = field(default_factory=list)
    recovery_used: bool = False
    stop_reason: StopReason | None = None
    policy_violations: list[dict] = field(default_factory=list)
    policy_fallbacks: int = 0


@dataclass(frozen=True, slots=True)
class PlannerContext:
    plan: EvidencePlan
    coverage: tuple[RequirementCoverage, ...]
    available_symbols: Mapping[str, SymbolObservation]
    symbol_requirements: Mapping[str, frozenset[str]]
    tool_history: tuple[ToolResult, ...]


@dataclass(slots=True)
class AgentRunState:
    query: str
    plan: EvidencePlan
    workspace: EvidenceWorkspace
    pool: EvidencePool
    task: TaskProgress = field(default_factory=TaskProgress)
    memory: AgentWorkingMemory = field(default_factory=AgentWorkingMemory)
    history: ExecutionHistory = field(default_factory=ExecutionHistory)
    control: RuntimeControl = field(default_factory=RuntimeControl)

    def planner_context(self):
        # Frozen dataclasses alone do not protect nested arguments dictionaries.
        # Recursively copy and wrap even malformed arguments retained in history;
        # all other ToolResult observation fields
        # are immutable tuples of frozen value objects.
        history = tuple(replace(r, call=replace(r.call, arguments=_freeze_arguments(r.call.arguments)))
                        for r in self.history.tool_history)
        return PlannerContext(self.plan, self.task.coverage,
            MappingProxyType(dict(self.memory.available_symbols)),
            MappingProxyType({k: frozenset(v) for k, v in self.memory.symbol_requirements.items()}), history)


@dataclass(frozen=True, slots=True)
class StepSnapshot:
    coverage: tuple[RequirementCoverage, ...]
    symbol_keys: frozenset[str]
    workspace: WorkspaceProgressSnapshot
