import time

from devcontext.agentic.evidence_models import EvidencePackage, package_state
from devcontext.agentic.retrieval_controller import RetrievalController, _stage_usage
from devcontext.agentic.retrieval_engine import RetrievalOutcome
from devcontext.context import EvidenceWorkspace
from devcontext.evidence import EvidencePool


class ToolDrivenRetrievalController:
    def __init__(self, evidence_planner, runtime, observer=None):
        self.evidence_planner = evidence_planner
        self.runtime = runtime
        self.observer = observer

    def retrieve(self, request, top_k):
        from devcontext.llm.errors import fatal_request_scope
        with fatal_request_scope():
            return self._retrieve(request, top_k)

    def _retrieve(self, request, top_k):
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        started = time.perf_counter()
        plan = self.evidence_planner.plan(request.original_query)
        stages = [_stage_usage("evidence_planning", started, plan.decision_source,
                  self.evidence_planner.last_client, "low")]
        workspace = EvidenceWorkspace(request.original_query)
        pool = EvidencePool()
        outcome = self.runtime.run(request.original_query, plan, workspace, pool)
        stages.extend(outcome.stages)
        bundle = RetrievalController._build_context(request, plan, pool, top_k)
        if self.observer:
            self.observer.on_context_built(max(0, len(outcome.memory.steps) - 1), bundle)
        coverage = outcome.memory.coverage
        state = package_state(plan, bundle, coverage, outcome.search_history,
            len(workspace) if request.answer_options.evidence_source == "workspace" else None,
            tool_summary=outcome.memory.execution_summary())
        package = EvidencePackage(request.original_query, plan, bundle, coverage,
            tuple(c.requirement_id for c in coverage if not c.satisfied), state, outcome.search_history,
            outcome.coverage_rounds, workspace.freeze(), workspace)
        return RetrievalOutcome(package, tuple(stages), outcome.trace())
