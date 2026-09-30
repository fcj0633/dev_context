from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from devcontext.models import Citation, SearchResult


# The workspace namespace. Legacy ContextBuilder labels stay on ``C`` so the two
# never collide: see ``context/builder.py`` where labels are assigned by position.
EVIDENCE_PREFIX = "E"


def evidence_label(index: int) -> str:
    return f"{EVIDENCE_PREFIX}{index}"


@dataclass(frozen=True, slots=True)
class EvidenceCatalog:
    """An immutable snapshot of every piece of evidence a run retrieved.

    This is the keying that section-scoped citation validation needs: one chunk
    has exactly one label for the whole run, regardless of which view or which
    answer section is looking at it.
    """

    by_evidence_id: Mapping[str, Citation]
    by_chunk_id: Mapping[int, str]

    def label_for(self, chunk_id: int) -> str | None:
        return self.by_chunk_id.get(chunk_id)

    def citation(self, evidence_id: str) -> Citation | None:
        return self.by_evidence_id.get(evidence_id)

    def labels(self) -> tuple[str, ...]:
        return tuple(self.by_evidence_id)

    def __len__(self) -> int:
        return len(self.by_evidence_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_count": len(self.by_evidence_id),
            "by_evidence_id": {
                label: citation.to_dict()
                for label, citation in self.by_evidence_id.items()
            },
            "by_chunk_id": {
                str(chunk_id): label
                for chunk_id, label in self.by_chunk_id.items()
            },
        }


@dataclass(slots=True)
class CitationRegistry:
    """Assigns each chunk one stable label for the lifetime of a ``retrieve()``.

    ``ContextBuilder`` numbers citations by their position in the bundle it is
    building, so the same chunk is ``C1`` in one bundle and ``C7`` in another.
    That makes a label useless as an identity. The registry assigns each chunk a
    label the first time it is seen and never changes it, including when a later
    retrieval round re-finds a chunk an earlier round already registered.
    """

    _citations: dict[str, Citation] = field(default_factory=dict)
    _by_chunk_id: dict[int, str] = field(default_factory=dict)
    _counter: int = 0
    _frozen: bool = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def register(self, result: SearchResult) -> str:
        """Return the chunk's stable label, assigning one if this is new."""
        existing = self._by_chunk_id.get(result.id)
        if existing is not None:
            return existing
        if self._frozen:
            raise RuntimeError("CitationRegistry is frozen; cannot register new evidence")
        self._counter += 1
        label = evidence_label(self._counter)
        self._citations[label] = citation_from_result(label, result)
        self._by_chunk_id[result.id] = label
        return label

    def register_many(self, results: Iterable[SearchResult]) -> dict[int, str]:
        return {result.id: self.register(result) for result in results}

    def label_for(self, chunk_id: int) -> str | None:
        return self._by_chunk_id.get(chunk_id)

    def citation(self, label: str) -> Citation | None:
        return self._citations.get(label)

    def labels(self) -> tuple[str, ...]:
        return tuple(self._citations)

    def __len__(self) -> int:
        return len(self._citations)

    def freeze(self) -> EvidenceCatalog:
        self._frozen = True
        return EvidenceCatalog(
            by_evidence_id=dict(self._citations),
            by_chunk_id=dict(self._by_chunk_id),
        )


def citation_from_result(label: str, result: SearchResult) -> Citation:
    return Citation(
        label=label,
        source_type=result.source_type,
        file_path=result.file_path,
        class_name=result.class_name,
        symbol_name=result.symbol_name,
        signature=result.signature,
        start_line=result.start_line,
        end_line=result.end_line,
        heading_path=list(result.heading_path),
    )
