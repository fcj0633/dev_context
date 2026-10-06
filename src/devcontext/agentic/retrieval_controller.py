from __future__ import annotations

import time
from dataclasses import replace
from typing import Mapping, Protocol

from devcontext.agentic.coverage import CoverageChecker
from devcontext.agentic.evidence_models import (
    CoverageRound,
    EvidencePackage,
    RequirementCoverage,
    SearchAction,
    package_state,
)
from devcontext.agentic.models import StageUsage, error_detail
from devcontext.agentic.search_actions import SearchActionPlanner
from devcontext.context import (
    ContextBuilder,
    EvidenceWorkspace,
    WorkspaceCoverageView,
)
from devcontext.evidence import EvidencePool, SourcePolicy
from devcontext.models import ContextBundle, SearchExecution
from devcontext.planning import EvidencePlan, EvidencePlanner, EvidenceRequirement
from devcontext.request import UserRequest
from devcontext.deadline import remaining_seconds
from devcontext.observability import RetrievalActionTrace, record_retrieval_action
from devcontext.retrieval import RetrievalPolicy
from devcontext.retrieval.symbols import code_strategy_for
from devcontext.agentic.retrieval_engine import RetrievalOutcome


COMPACT_CONTEXT_BUDGET = 8_000
BALANCED_CONTEXT_BUDGET = 16_000
BROAD_CONTEXT_BUDGET = 28_000
MAX_SEARCH_ACTIONS = 12
V3_TEACHING_RESERVE_SECONDS = 120
FOLLOWUP_ESTIMATED_SECONDS = 35
CORE_TOP_K = 5
SUPPORTING_TOP_K = 3


class _GraphEvidencePool(EvidencePool):
    """Keep original search candidates ahead of graph supplements across rounds."""
    def __init__(self):
        super().__init__()
        self.original_candidates = {}

    def prioritize_original(self, requirement_id, candidates):
        original = self.original_candidates.setdefault(requirement_id, {})
        for candidate in candidates:
            original.setdefault(candidate.search_result.id, candidate)
        self.candidates_by_sub_question[requirement_id] = list(original.values()) + [
            item for item in self.candidates_by_sub_question[requirement_id]
            if item.search_result.id not in original
        ]


class RetrievalObserver(Protocol):
    """Read-only callbacks for diagnostics and offline evaluation."""

    def on_action_completed(
        self, action: SearchAction, execution: SearchExecution
    ) -> None: ...

    def on_context_built(
        self, round_index: int, context: ContextBundle
    ) -> None: ...


class RetrievalController:
    """Bounded evidence retrieval state machine.

    EvidencePlanner owns *what facts are required*.  This controller owns *how
    to find them*, and freezes an EvidencePackage before answer writing begins.
    """

    def __init__(
        self,
        evidence_planner: EvidencePlanner,
        action_planner: SearchActionPlanner,
        retrieval_policy: RetrievalPolicy,
        coverage_checker: CoverageChecker,
        source_policy: SourcePolicy,
        observer: RetrievalObserver | None = None,
        graph_expander=None,
    ) -> None:
        self.evidence_planner = evidence_planner
        self.action_planner = action_planner
        self.retrieval_policy = retrieval_policy
        self.coverage_checker = coverage_checker
        self.source_policy = source_policy
        self.observer = observer
        self.graph_expander = graph_expander

    def retrieve(self, request: UserRequest, top_k: int) -> RetrievalOutcome:
        if top_k < 1 or top_k > 100:
            raise ValueError("top_k must be between 1 and 100")
        stages: list[StageUsage] = []

        started = time.perf_counter()
        plan = self.evidence_planner.plan(request.original_query)
        stages.append(
            _stage_usage(
                "evidence_planning",
                started,
                plan.decision_source,
                self.evidence_planner.last_client,
                "high",
            )
        )

        pool = _GraphEvidencePool() if self.graph_expander is not None else EvidencePool()
        # Request-local original candidates retain presentation precedence across
        # both rounds. Otherwise early graph hits can crowd out later real search
        # hits in the fixed-size answer bundle, despite improving the workspace.
        # Held for the whole lifecycle: evidence ids are assigned once and never
        # renumbered, so a chunk means the same thing in every later view.
        workspace = EvidenceWorkspace(request.original_query)
        history: list[SearchAction] = []
        coverage_rounds: list[CoverageRound] = []

        first_requirements = tuple(
            sorted(
                plan.requirements,
                key=lambda item: (item.priority != "CORE", int(item.id[2:])),
            )
        )
        action_started = time.perf_counter()
        first_actions = self.action_planner.plan_actions(
            request.original_query,
            first_requirements,
            round_index=0,
        )
        stages.append(
            _stage_usage(
                "search_action_planning",
                action_started,
                _decision_source(first_actions),
                self.action_planner.last_client,
                "low",
                round_index=0,
            )
        )
        retrieval_started = time.perf_counter()
        history.extend(self._execute(first_actions, plan, pool, workspace, 0))
        stages.append(
            StageUsage(
                "evidence_retrieval",
                (time.perf_counter() - retrieval_started) * 1000,
                "policy",
                round_index=0,
            )
        )

        bundle = self._build_context(request, plan, pool, top_k)
        if self.observer is not None:
            self.observer.on_context_built(0, bundle)
        check_started = time.perf_counter()
        # Judged against the workspace, not the answer bundle: judging against the
        # bundle made a requirement look MISSING when the presentation budget had
        # simply dropped its evidence.
        coverage = self.coverage_checker.check(
            plan.requirements, WorkspaceCoverageView(workspace, round_index=0)
        )
        coverage_rounds.append(CoverageRound(0, coverage))
        stages.append(
            _stage_usage(
                "coverage_check",
                check_started,
                _coverage_source(coverage),
                self.coverage_checker.last_client,
                "low",
                round_index=0,
            )
        )

        coverage_by_id = {item.requirement_id: item for item in coverage}
        followup_requirements = tuple(
            item
            for item in plan.requirements
            if item.priority == "CORE"
            and coverage_by_id[item.id].state in {"PARTIAL", "MISSING"}
        )
        remaining_budget = MAX_SEARCH_ACTIONS - len(history)
        followup_requirements = followup_requirements[:remaining_budget]
        remaining = None if request.policy else remaining_seconds()
        if request.policy is None and followup_requirements and remaining is not None and remaining < V3_TEACHING_RESERVE_SECONDS + FOLLOWUP_ESTIMATED_SECONDS:
            # Reserve 120s for teaching plus 35s for a possible follow-up.
            # A shared deadline is installed only for V3. Preserve partial
            # coverage as-is; never upgrade missing evidence to satisfied.
            stages.append(StageUsage("retrieval_followup_skipped", 0.0, "answer_time_reserve"))
            followup_requirements = ()
        if followup_requirements:
            action_started = time.perf_counter()
            followup_actions = self.action_planner.plan_actions(
                request.original_query,
                followup_requirements,
                round_index=1,
                history=tuple(history),
                coverage=coverage_by_id,
                discovered_terms=_discovered_terms(workspace, followup_requirements),
            )
            stages.append(
                _stage_usage(
                    "search_action_planning",
                    action_started,
                    _decision_source(followup_actions),
                    self.action_planner.last_client,
                    "low",
                    round_index=1,
                )
            )
            retrieval_started = time.perf_counter()
            history.extend(self._execute(followup_actions, plan, pool, workspace, 1))
            stages.append(
                StageUsage(
                    "evidence_retrieval",
                    (time.perf_counter() - retrieval_started) * 1000,
                    "policy",
                    round_index=1,
                )
            )
            bundle = self._build_context(request, plan, pool, top_k)
            if self.observer is not None:
                self.observer.on_context_built(1, bundle)
            check_started = time.perf_counter()
            coverage = self.coverage_checker.check(
                plan.requirements, WorkspaceCoverageView(workspace, round_index=1)
            )
            coverage_rounds.append(CoverageRound(1, coverage))
            stages.append(
                _stage_usage(
                    "coverage_check",
                    check_started,
                    _coverage_source(coverage),
                    self.coverage_checker.last_client,
                    "low",
                    round_index=1,
                )
            )

        state = package_state(
            plan,
            bundle,
            coverage,
            tuple(history),
            len(workspace)
            if request.answer_options.evidence_source == "workspace"
            else None,
        )
        unresolved = tuple(
            item.requirement_id for item in coverage if not item.satisfied
        )
        package = EvidencePackage(
            request.original_query,
            plan,
            bundle,
            coverage,
            unresolved,
            state,
            tuple(history),
            tuple(coverage_rounds),
            workspace.freeze(),
            workspace,
        )
        return RetrievalOutcome(package, tuple(stages))

    def _execute(
        self,
        actions: tuple[SearchAction, ...],
        plan: EvidencePlan,
        pool: EvidencePool,
        workspace: EvidenceWorkspace,
        round_index: int,
    ) -> tuple[SearchAction, ...]:
        by_id = {item.id: item for item in plan.requirements}
        executed: list[SearchAction] = []
        for action in actions:
            from devcontext.deadline import remaining_seconds, RequestDeadlineExceeded
            try:
                remaining_seconds()
            except RequestDeadlineExceeded:
                break
            if len(executed) >= MAX_SEARCH_ACTIONS:
                break
            requirement = by_id[action.requirement_id]
            per_action_top_k = (
                CORE_TOP_K if requirement.priority == "CORE" else SUPPORTING_TOP_K
            )
            # Stage-aware CODE routing. Round 0 usually has nothing but business
            # semantics to go on, so there is no exact term for a keyword half to
            # match and hybrid only adds its ranking noise; round 1 usually has
            # real class/method names discovered from round-0 evidence, which is
            # exactly what keyword search is good at. The decision is driven by
            # whether a real symbol exists, never by the round number as such.
            code_strategy: str | None = None
            symbols: tuple[str, ...] = ()
            if action.source_scope == "CODE":
                code_strategy, symbols = code_strategy_for(
                    action.query, workspace, action.requirement_id
                )
            try:
                execution = self.retrieval_policy.search_scope_with_trace(
                    action.query,
                    action.source_scope,
                    per_action_top_k,
                    code_strategy=code_strategy,
                )
                if code_strategy is not None:
                    execution.symbols = symbols
                if self.graph_expander is not None:
                    expansion = self.graph_expander.expand(action, requirement, execution, round_index)
                    execution.graph_results = expansion.results
                    execution.graph_trace = expansion.trace.to_dict()
                record_retrieval_action(
                    _action_trace(action, execution, round_index, per_action_top_k)
                )
                annotated = [
                    self.source_policy.classify(result, requirement.id)
                    for result in execution.results + execution.graph_results
                ]
                pool.add_many(annotated)
                if isinstance(pool, _GraphEvidencePool):
                    pool.prioritize_original(requirement.id, [
                        self.source_policy.classify(result, requirement.id) for result in execution.results
                    ])
                # Register + ingest per round, so a round's snapshot reflects only
                # what that round found rather than everything found so far.
                workspace.ingest(annotated, round_index)
                if self.observer is not None:
                    self.observer.on_action_completed(action, execution)
                executed.append(action)
            except Exception as exception:
                detail = error_detail(exception)
                # A failed action is the one case where the retrieval split
                # matters most: it says whether the embedding endpoint, the SQL,
                # or the fusion is what did not answer.
                record_retrieval_action(
                    RetrievalActionTrace(
                        action_id=action.action_id,
                        requirement_id=action.requirement_id,
                        round_index=round_index,
                        query=action.query,
                        source_scope=action.source_scope,
                        top_k=per_action_top_k,
                        total_ms=0.0,
                        error=detail,
                    )
                )
                executed.append(replace(action, error=detail))
        return tuple(executed)

    @staticmethod
    def _build_context(
        request: UserRequest,
        plan: EvidencePlan,
        pool: EvidencePool,
        top_k: int,
    ) -> ContextBundle:
        results, annotations = pool.select(plan.requirements, top_k)
        budget = request.context_budget_override or context_budget_for_plan(plan)
        return ContextBuilder(max_chars=budget).build(
            request.original_query, results, annotations
        )


def context_budget_for_plan(plan: EvidencePlan) -> int:
    requirements = plan.requirements
    if (
        len(requirements) == 1
        and requirements[0].priority == "CORE"
        and requirements[0].source_requirement != "BOTH"
    ):
        return COMPACT_CONTEXT_BUDGET
    both_count = sum(
        item.source_requirement == "BOTH" for item in requirements
    )
    current_core_count = sum(
        item.priority == "CORE" and item.temporal_scope == "CURRENT"
        for item in requirements
    )
    if len(requirements) >= 4 or both_count >= 2 or current_core_count >= 2:
        return BROAD_CONTEXT_BUDGET
    return BALANCED_CONTEXT_BUDGET


def _discovered_terms(
    workspace: EvidenceWorkspace,
    requirements: tuple[EvidenceRequirement, ...],
) -> Mapping[str, tuple[str, ...]]:
    """Seed the second round from everything found so far, not from the bundle.

    Reading the bundle meant an identifier that fell past the presentation budget
    could never be used to look for more, so the budget quietly degraded the
    second round as well as the answer.
    """
    requested = {item.id for item in requirements}
    found: dict[str, list[str]] = {item: [] for item in requested}
    for ref in workspace.for_requirements(sorted(requested)):
        citation = ref.citation
        identities = tuple(
            value
            for value in (
                citation.class_name,
                citation.symbol_name,
                citation.signature,
                " > ".join(citation.heading_path),
                citation.file_path,
            )
            if value
        )
        for requirement_id in ref.requirement_ids:
            if requirement_id not in requested:
                continue
            for identity in identities:
                if identity not in found[requirement_id]:
                    found[requirement_id].append(identity)
    return {key: tuple(value[:8]) for key, value in found.items()}


def _decision_source(actions: tuple[SearchAction, ...]) -> str:
    return "llm" if actions and all(item.decision_source == "llm" for item in actions) else "fallback"


def _coverage_source(coverage: tuple[RequirementCoverage, ...]) -> str:
    if any(item.state == "UNVERIFIED" for item in coverage):
        return "fallback"
    if any(item.decision_source == "llm" for item in coverage):
        return "llm"
    return "rules"


def _stage_usage(
    stage: str,
    started: float,
    decision_source: str,
    client: object | None,
    default_effort: str,
    *,
    round_index: int | None = None,
) -> StageUsage:
    usage = getattr(client, "last_usage", {}) if client is not None else {}
    if not isinstance(usage, dict):
        usage = {}
    return StageUsage(
        stage,
        (time.perf_counter() - started) * 1000,
        decision_source,
        getattr(client, "model", None),
        getattr(client, "reasoning_effort", default_effort),
        usage.get("prompt_tokens", usage.get("input_tokens")),
        usage.get("completion_tokens", usage.get("output_tokens")),
        round_index,
    )


def _action_trace(
    action: SearchAction,
    execution: SearchExecution,
    round_index: int,
    top_k: int,
) -> RetrievalActionTrace:
    """One search action's cost, split by pipeline part rather than left as a total."""
    timings = execution.timings
    return RetrievalActionTrace(
        action_id=action.action_id,
        requirement_id=action.requirement_id,
        round_index=round_index,
        query=action.query,
        source_scope=action.source_scope,
        top_k=top_k,
        total_ms=timings.total_ms,
        result_count=len(execution.results),
        query_embedding_ms=timings.query_embedding_ms,
        keyword_sql_ms=timings.keyword_sql_ms,
        vector_sql_ms=timings.vector_sql_ms,
        fusion_ms=timings.fusion_ms,
    )
