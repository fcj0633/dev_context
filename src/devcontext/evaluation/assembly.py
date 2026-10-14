"""Translate evaluation fixtures into explicit production component overrides."""
from devcontext.application.assembly import RetrievalOverrides
from .retrieval_workflow_runner import FrozenEvidencePlanner, FrozenSearchActionPlanner, OracleCoverageChecker


def retrieval_overrides(case=None, coverage_factory=None):
    if case is None and coverage_factory is None:
        return None
    return RetrievalOverrides(
        FrozenEvidencePlanner(case) if case is not None else None,
        FrozenSearchActionPlanner(case) if case is not None else None,
        coverage_factory() if coverage_factory is not None else OracleCoverageChecker(case) if case is not None else None,
    )
