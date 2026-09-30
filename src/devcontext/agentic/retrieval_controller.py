from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Mapping, Protocol

from devcontext.agentic.coverage import CoverageChecker
from devcontext.agentic.evidence_models import (
    CoverageRound,
    EvidencePackage,
    RequirementCoverage,
    SearchAction,
    package_state,
)
from devcontext.agentic.models import StageUsage
from devcontext.agentic.search_actions import SearchActionPlanner
from devcontext.context import ContextBuilder, EvidenceWorkspace
from devcontext.evidence import EvidencePool, SourcePolicy
from devcontext.models import ContextBundle, SearchExecution
from devcontext.planning import EvidencePlan, EvidencePlanner, EvidenceRequirement
from devcontext.request import UserRequest
from devcontext.retrieval import RetrievalPolicy


COMPACT_CONTEXT_BUDGET = 8_000
BALANCED_CONTEXT_BUDGET = 16_000
BROAD_CONTEXT_BUDGET = 28_000
MAX_SEARCH_ACTIONS = 12
CORE_TOP_K = 5
SUPPORTING_TOP_K = 3


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    package: EvidencePackage
    stage_usage: tuple[StageUsage, ...]


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
    ) -> None:
        self.evidence_planner = evidence_planner
        self.action_planner = action_planner
        self.retrieval_policy = retrieval_policy
        self.coverage_checker = coverage_checker
        self.source_policy = source_policy
        self.observer = observer

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

        pool = EvidencePool()
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
            )
        )
        retrieval_started = time.perf_counter()
        history.extend(self._execute(first_actions, plan, pool, workspace, 0))
        stages.append(
            StageUsage(
                "evidence_retrieval",
                (time.perf_counter() - retrieval_started) * 1000,
                "policy",
            )
        )

        bundle = self._build_context(request, plan, pool, top_k)
        if self.observer is not None:
            self.observer.on_context_built(0, bundle)
        check_started = time.perf_counter()
        coverage = self.coverage_checker.check(plan.requirements, bundle)
        coverage_rounds.append(CoverageRound(0, coverage))
        stages.append(
            _stage_usage(
                "coverage_check",
                check_started,
                _coverage_source(coverage),
                self.coverage_checker.last_client,
                "low",
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
        if followup_requirements:
            action_started = time.perf_counter()
            followup_actions = self.action_planner.plan_actions(
                request.original_query,
                followup_requirements,
                round_index=1,
                history=tuple(history),
                coverage=coverage_by_id,
                discovered_terms=_discovered_terms(bundle, followup_requirements),
            )
            stages.append(
                _stage_usage(
                    "search_action_planning",
                    action_started,
                    _decision_source(followup_actions),
                    self.action_planner.last_client,
                    "low",
                )
            )
            retrieval_started = time.perf_counter()
            history.extend(self._execute(followup_actions, plan, pool, workspace, 1))
            stages.append(
                StageUsage(
                    "evidence_retrieval",
                    (time.perf_counter() - retrieval_started) * 1000,
                    "policy",
                )
            )
            bundle = self._build_context(request, plan, pool, top_k)
            if self.observer is not None:
                self.observer.on_context_built(1, bundle)
            check_started = time.perf_counter()
            coverage = self.coverage_checker.check(plan.requirements, bundle)
            coverage_rounds.append(CoverageRound(1, coverage))
            stages.append(
                _stage_usage(
                    "coverage_check",
                    check_started,
                    _coverage_source(coverage),
                    self.coverage_checker.last_client,
                    "low",
                )
            )

        state = package_state(plan, bundle, coverage)
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
            if len(executed) >= MAX_SEARCH_ACTIONS:
                break
            requirement = by_id[action.requirement_id]
            per_action_top_k = (
                CORE_TOP_K if requirement.priority == "CORE" else SUPPORTING_TOP_K
            )
            try:
                execution = self.retrieval_policy.search_scope_with_trace(
                    action.query,
                    action.source_scope,
                    per_action_top_k,
                )
                annotated = [
                    self.source_policy.classify(result, requirement.id)
                    for result in execution.results
                ]
                pool.add_many(annotated)
                # Register + ingest per round, so a round's snapshot reflects only
                # what that round found rather than everything found so far.
                workspace.ingest(annotated, round_index)
                if self.observer is not None:
                    self.observer.on_action_completed(action, execution)
                executed.append(action)
            except Exception as exception:
                executed.append(
                    replace(action, error=type(exception).__name__)
                )
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
    bundle: ContextBundle,
    requirements: tuple[EvidenceRequirement, ...],
) -> Mapping[str, tuple[str, ...]]:
    requested = {item.id for item in requirements}
    found: dict[str, list[str]] = {item: [] for item in requested}
    for item in bundle.items:
        identities = tuple(
            value
            for value in (
                item.citation.class_name,
                item.citation.symbol_name,
                item.citation.signature,
                " > ".join(item.citation.heading_path),
                item.citation.file_path,
            )
            if value
        )
        for requirement_id in item.sub_question_ids:
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
    )
