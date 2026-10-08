from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from devcontext.context.registry import (
    CitationRegistry,
    EvidenceCatalog,
    citation_from_result,
)
from devcontext.evidence import EvidenceCandidate
from devcontext.models import Citation
from devcontext.context.relations import EvidenceSymbol, EvidenceRelation, RelationOwnership, RelationProvenance, RelationProbe, ConfirmedPath, metadata


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """One piece of retrieved evidence, with no size ceiling attached.

    ``truncated`` deliberately does not appear here. Truncation is a property of
    a prompt-sized view, not of the evidence itself; keeping it out is what lets
    the workspace hold more than any single prompt could.
    """

    chunk_id: int
    evidence_id: str
    citation: Citation
    requirement_ids: tuple[str, ...]
    source_role: str
    temporal_status: str
    authority_priority: int
    chunk_type: str
    # The round that first produced this chunk; it does not change when a later
    # round re-finds the same chunk.
    first_seen_round: int
    # Rank within the search action that produced this chunk, not the rank it
    # would get in a ContextBundle - the bundle re-ranks across all actions.
    retrieval_rank: int
    retrieval_score: float
    content: str | None = None

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "evidence_id": self.evidence_id,
            "citation": self.citation.to_dict(),
            "requirement_ids": list(self.requirement_ids),
            "source_role": self.source_role,
            "temporal_status": self.temporal_status,
            "authority_priority": self.authority_priority,
            "chunk_type": self.chunk_type,
            "first_seen_round": self.first_seen_round,
            "retrieval_rank": self.retrieval_rank,
            "retrieval_score": self.retrieval_score,
        }
        if include_content:
            value["content"] = self.content
        return value


@dataclass(slots=True)
class EvidenceWorkspace:
    """Everything retrieval found, addressable by requirement, round or label.

    ``ContextBuilder`` applies one character budget and stops at the first chunk
    that does not fit, so evidence past that point is gone for every later
    stage. The workspace is the opposite: it holds the full set and lets each
    consumer ask for the slice it needs.
    """

    original_query: str = ""
    registry: CitationRegistry = field(default_factory=CitationRegistry)
    _refs: dict[int, EvidenceRef] = field(default_factory=dict)
    _by_requirement: dict[str, list[int]] = field(default_factory=dict)
    _order: list[int] = field(default_factory=list)
    _added_per_round: dict[int, int] = field(default_factory=dict)
    _frozen: bool = False
    _symbols: dict[str, EvidenceSymbol] = field(default_factory=dict)
    _symbol_ownership: dict[tuple[str, str], int] = field(default_factory=dict)
    _relations: dict[tuple, EvidenceRelation] = field(default_factory=dict)
    _relation_ownership: dict[tuple, RelationOwnership] = field(default_factory=dict)
    _relation_provenance: set[RelationProvenance] = field(default_factory=set)
    _probes: list[RelationProbe] = field(default_factory=list)
    _paths: dict[tuple[str, int], ConfirmedPath] = field(default_factory=dict)
    _chunk_ownership_round: dict[tuple[str, int], int] = field(default_factory=dict)

    def _writable(self):
        if self._frozen:
            raise RuntimeError('EvidenceWorkspace is frozen')

    def add_symbols(self, symbols, requirement_id, round_index):
        self._writable()
        for symbol in symbols:
            if self.get(symbol.chunk_id) is None or requirement_id not in self.get(symbol.chunk_id).requirement_ids:
                raise ValueError('Symbol endpoint is not owned by this requirement')
            previous = self._symbols.get(symbol.symbol_key)
            if previous is not None and previous != symbol:
                raise ValueError('Conflicting indexed symbol identity')
            self._symbols[symbol.symbol_key] = symbol
            self._symbol_ownership.setdefault((requirement_id, symbol.symbol_key), round_index)

    def symbols_for(self, requirement_id, round_index=None):
        return tuple(s for key, s in self._symbols.items() if (requirement_id, key) in self._symbol_ownership
                     and (round_index is None or self._symbol_ownership[requirement_id, key] <= round_index))

    def add_relations(self, relations, requirement_id, round_index, source, observation_id=''):
        self._writable()
        new_relations = new_ownership = 0
        for relation in relations:
            keys = {s.symbol_key: s for s in self.symbols_for(requirement_id, round_index)}
            if relation.source not in keys or relation.target not in keys:
                raise ValueError('Relation endpoints must be confirmed and requirement-scoped')
            if (keys[relation.source].chunk_id, keys[relation.target].chunk_id) != (relation.source_chunk_id, relation.target_chunk_id):
                raise ValueError('Relation endpoint mapping is invalid')
            identity = relation.identity
            if identity not in self._relations:
                self._relations[identity] = relation
                new_relations += 1
            ownership_key = (requirement_id, identity)
            if ownership_key not in self._relation_ownership:
                self._relation_ownership[ownership_key] = RelationOwnership(identity, requirement_id, round_index)
                new_ownership += 1
            self._relation_provenance.add(RelationProvenance(identity, source, observation_id, round_index))
        return new_relations, new_ownership

    def relations_for(self, requirement_id, round_index=None):
        return tuple(self._relations[identity] for (rid, identity), owner in self._relation_ownership.items()
                     if rid == requirement_id and (round_index is None or owner.first_seen_round <= round_index))

    def add_probe(self, probe):
        self._writable()
        self._probes.append(probe)

    def probes_for(self, requirement_id, round_index=None):
        return tuple(p for p in self._probes if p.requirement_id == requirement_id and (round_index is None or p.round_index <= round_index))

    def add_path(self, path):
        self._writable()
        self._paths[path.requirement_id, path.need_index] = path

    def structural_metadata(self):
        return {'symbols': [metadata(s) for s in self._symbols.values()],
                'symbol_ownership': [{'requirement_id': r, 'symbol_key': k, 'round_index': v} for (r, k), v in self._symbol_ownership.items()],
                'relations': [metadata(r) for r in self._relations.values()],
                'ownership': [metadata(o) for o in self._relation_ownership.values()],
                'provenance': [metadata(p) for p in sorted(self._relation_provenance, key=repr)],
                'probes': [metadata(p) for p in self._probes], 'paths': [metadata(p) for p in self._paths.values()]}

    @property
    def frozen(self) -> bool:
        return self._frozen

    def ingest(
        self,
        candidates: Iterable[EvidenceCandidate],
        round_index: int = 0,
    ) -> tuple[int, ...]:
        """Add a retrieval round's candidates. Returns the newly added chunk ids.

        A chunk found by a later round keeps its earlier evidence id, and the new
        requirement is merged into it rather than replacing the old one.
        """
        if self._frozen:
            raise RuntimeError("EvidenceWorkspace is frozen; cannot ingest more evidence")
        added: list[int] = []
        for rank, candidate in enumerate(candidates, start=1):
            result = candidate.search_result
            self._chunk_ownership_round.setdefault((candidate.sub_question_id, result.id), round_index)
            evidence_id = self.registry.register(result)
            existing = self._refs.get(result.id)
            if existing is not None:
                requirement_ids = _merge(existing.requirement_ids, candidate.sub_question_id)
                if requirement_ids != existing.requirement_ids:
                    self._refs[result.id] = _replace_requirements(existing, requirement_ids)
                    self._by_requirement.setdefault(candidate.sub_question_id, []).append(result.id)
                continue
            self._refs[result.id] = EvidenceRef(
                chunk_id=result.id,
                evidence_id=evidence_id,
                citation=citation_from_result(evidence_id, result),
                requirement_ids=(candidate.sub_question_id,),
                source_role=candidate.source_role,
                temporal_status=candidate.temporal_status,
                authority_priority=candidate.authority_priority,
                chunk_type=result.chunk_type,
                first_seen_round=round_index,
                retrieval_rank=rank,
                retrieval_score=result.score,
                content=result.content,
            )
            self._order.append(result.id)
            self._added_per_round[round_index] = self._added_per_round.get(round_index, 0) + 1
            self._by_requirement.setdefault(candidate.sub_question_id, []).append(result.id)
            added.append(result.id)
        return tuple(added)

    def get(self, chunk_id: int) -> EvidenceRef | None:
        return self._refs.get(chunk_id)

    def for_requirement(self, requirement_id: str) -> tuple[EvidenceRef, ...]:
        return self._collect(self._by_requirement.get(requirement_id, ()))

    def for_requirements(self, requirement_ids: Iterable[str]) -> tuple[EvidenceRef, ...]:
        seen: list[int] = []
        for requirement_id in requirement_ids:
            for chunk_id in self._by_requirement.get(requirement_id, ()):
                if chunk_id not in seen:
                    seen.append(chunk_id)
        return self._collect(seen)

    def for_round(self, round_index: int) -> tuple[EvidenceRef, ...]:
        """Everything known as of that round, cumulatively.

        Cumulative rather than disjoint on purpose: the legacy controller judges
        round 1's coverage against the whole pool, so a round-1 snapshot that
        could not see round 0's evidence would be a behaviour change. What this
        must never do is show evidence a later round has not found yet.
        """
        return tuple(
            ref
            for ref in self._collect(self._order)
            if ref.first_seen_round <= round_index
        )

    def by_citation(self, labels: Iterable[str]) -> tuple[EvidenceRef, ...]:
        wanted = set(labels)
        return tuple(
            ref for ref in self._collect(self._order) if ref.evidence_id in wanted
        )

    def all(self) -> tuple[EvidenceRef, ...]:
        return self._collect(self._order)

    def materialize(self, chunk_ids: Iterable[int]) -> tuple[EvidenceRef, ...]:
        """Return refs with their content attached.

        Content is captured at ingest time today, so this is a no-op that marks
        the seam where a ChunkStore lookup would go once the workspace starts
        holding references rather than bodies.
        """
        return self._collect(chunk_ids)

    def metadata_view(self) -> tuple[dict[str, Any], ...]:
        """Everything except the bodies, for cheap prompt sizing and tracing."""
        return tuple(ref.to_dict() for ref in self._collect(self._order))

    def requirement_ids(self) -> tuple[str, ...]:
        return tuple(self._by_requirement)

    def stats(self) -> dict[str, Any]:
        return {
            "evidence_count": len(self._refs),
            "requirement_count": len(self._by_requirement),
            "round_count": len(self._added_per_round),
            "added_per_round": {
                str(index): count for index, count in sorted(self._added_per_round.items())
            },
            "total_content_chars": sum(
                len(ref.content or "") for ref in self._refs.values()
            ),
        }

    def freeze(self) -> EvidenceCatalog:
        self._frozen = True
        return self.registry.freeze()

    def __len__(self) -> int:
        return len(self._refs)

    def _collect(self, chunk_ids: Iterable[int]) -> tuple[EvidenceRef, ...]:
        refs = []
        for chunk_id in chunk_ids:
            ref = self._refs.get(chunk_id)
            if ref is not None:
                refs.append(ref)
        return tuple(refs)


def _merge(existing: tuple[str, ...], requirement_id: str) -> tuple[str, ...]:
    if requirement_id in existing:
        return existing
    return (*existing, requirement_id)


def _replace_requirements(ref: EvidenceRef, requirement_ids: tuple[str, ...]) -> EvidenceRef:
    return EvidenceRef(
        chunk_id=ref.chunk_id,
        evidence_id=ref.evidence_id,
        citation=ref.citation,
        requirement_ids=requirement_ids,
        source_role=ref.source_role,
        temporal_status=ref.temporal_status,
        authority_priority=ref.authority_priority,
        chunk_type=ref.chunk_type,
        first_seen_round=ref.first_seen_round,
        retrieval_rank=ref.retrieval_rank,
        retrieval_score=ref.retrieval_score,
        content=ref.content,
    )
