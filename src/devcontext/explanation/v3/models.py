from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from devcontext.models import ContextBundle


@dataclass(frozen=True, slots=True)
class TeachingBlueprint:
    data: dict[str, Any]
    warnings: tuple[str, ...] = ()
    decision_source: str = "llm"
    conflicts: tuple = ()

    @property
    def answer_depth(self):
        return self.data["answer_depth"]

    @property
    def question_kind(self):
        return self.data["question_kind"]

    @property
    def sections(self):
        return self.data["answer_structure"]

    def to_dict(self):
        result = deepcopy(self.data)
        result["knowledge_boundary"] = {
            key: [c["id"] for c in result["claims"] if c["status"] == status]
            for key, status in (("confirmed", "CONFIRMED"), ("inference", "INFERENCE"), ("unknown", "UNKNOWN"))
        }
        for section in result["answer_structure"]:
            section["owned_claims"] = [c["id"] for c in result["claims"] if c["owner_section"] == section["id"]]
        result["warnings"] = list(self.warnings)
        return result


@dataclass(frozen=True, slots=True)
class WriterSection:
    id: str
    title: str
    evidence_labels: tuple[str, ...]
    contract: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WriterEvidencePack:
    sections: tuple[WriterSection, ...]
    catalog: tuple[dict[str, Any], ...]
    bundle: ContextBundle

    def to_dict(self):
        return {
            "section_contracts": [deepcopy(s.contract) for s in self.sections],
            "catalog": deepcopy(list(self.catalog)),
            "size": {"labels": len(self.catalog), "chars": self.bundle.total_chars},
        }
