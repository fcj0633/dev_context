from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


EVIDENCE_PLAN_SCHEMA_VERSION = 2
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EvidencePlan:
    original_query: str
    requirements: tuple[EvidenceRequirement, ...]
    decision_source: str = "llm"
    schema_version: int = EVIDENCE_PLAN_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "original_query": self.original_query,
            "requirements": [item.to_dict() for item in self.requirements],
            "decision_source": self.decision_source,
        }
