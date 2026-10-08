from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any


from devcontext.planning.retrieval_need import RetrievalNeed, legacy_needs

EVIDENCE_PLAN_SCHEMA_VERSION = 3
EVIDENCE_PRIORITIES = ("CORE", "SUPPORTING")
EVIDENCE_TEMPORAL_SCOPES = ("CURRENT", "HISTORY", "FUTURE", "ANY")
SOURCE_REQUIREMENTS = ("CODE", "DOCUMENT", "BOTH", "ANY")


@dataclass(frozen=True, slots=True)
class EvidenceRequirement:
    id: str
    target: str
    success_criteria: str
    priority: str
    temporal_scope: str
    source_requirement: str
    retrieval_needs: tuple[RetrievalNeed, ...] = ()

    def __post_init__(self):
        if not isinstance(self.retrieval_needs, tuple) or any(not isinstance(n, RetrievalNeed) for n in self.retrieval_needs):
            raise ValueError('retrieval_needs must be immutable contracts')

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvidencePlan:
    original_query: str
    requirements: tuple[EvidenceRequirement, ...]
    decision_source: str = "llm"
    schema_version: int = EVIDENCE_PLAN_SCHEMA_VERSION

    def __post_init__(self):
        # This constructor is the unified legacy import seam. All consumers see v3.
        if self.schema_version not in {2, 3} or not isinstance(self.requirements, tuple):
            raise ValueError('Invalid evidence plan version or mutable requirements')
        normalized = tuple(r if r.retrieval_needs else replace(r, retrieval_needs=legacy_needs(r.target, r.success_criteria, r.source_requirement)) for r in self.requirements)
        object.__setattr__(self, 'requirements', normalized)
        object.__setattr__(self, 'schema_version', 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "original_query": self.original_query,
            "requirements": [item.to_dict() for item in self.requirements],
            "decision_source": self.decision_source,
        }
