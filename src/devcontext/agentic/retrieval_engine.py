"""The evidence boundary shared by fixed and tool-driven retrieval."""
from dataclasses import dataclass
from typing import Any, Protocol

from devcontext.agentic.evidence_models import EvidencePackage
from devcontext.agentic.models import StageUsage
from devcontext.request import UserRequest
from devcontext.planning import EvidencePlan


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    package: EvidencePackage
    stage_usage: tuple[StageUsage, ...]
    agent_trace: dict[str, Any] | None = None


class RetrievalEngine(Protocol):
    def retrieve(self, request: UserRequest, top_k: int) -> RetrievalOutcome: ...


class FixedEvidencePlanner:
    """Reuse the identical immutable plan across all arms of a paired run."""
    last_client = None

    def __init__(self, plan: EvidencePlan):
        self.frozen_plan = plan

    def plan(self, query: str) -> EvidencePlan:
        if query != self.frozen_plan.original_query:
            raise ValueError("Frozen evidence plan belongs to a different query")
        return self.frozen_plan
