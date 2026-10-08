"""Request-local structural evidence; physical edges never change direction."""
from dataclasses import asdict, dataclass
from devcontext.planning.retrieval_need import EDGES


@dataclass(frozen=True, slots=True)
class EvidenceSymbol:
    symbol_key: str
    symbol_kind: str
    chunk_id: int
    state: str = 'CONFIRMED'

    def __post_init__(self):
        if self.state != 'CONFIRMED' or self.symbol_kind not in {'CLASS', 'INTERFACE', 'METHOD', 'CONSTRUCTOR'}:
            raise ValueError('Only confirmed indexed symbols are structural evidence')


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    repository: str
    source: str
    target: str
    edge_type: str
    resolution_kind: str
    source_line: int
    source_column: int
    source_chunk_id: int
    target_chunk_id: int

    def __post_init__(self):
        if self.edge_type not in EDGES or self.resolution_kind not in {'AST_EXACT', 'SYMBOL_SOLVER_EXACT', 'DERIVED_EXACT'}:
            raise ValueError('Relation requires a reliable physical indexed edge')
        if min(self.source_line, self.source_column, self.source_chunk_id, self.target_chunk_id) <= 0:
            raise ValueError('Relation requires valid indexed endpoints and location')

    @property
    def identity(self):
        return (self.repository, self.source, self.target, self.edge_type, self.source_line, self.source_column)


@dataclass(frozen=True, slots=True)
class RelationOwnership:
    relation_id: tuple
    requirement_id: str
    first_seen_round: int


@dataclass(frozen=True, slots=True)
class RelationProvenance:
    relation_id: tuple
    source: str
    observation_id: str
    round_index: int


@dataclass(frozen=True, slots=True)
class RelationProbe:
    requirement_id: str
    need_indices: tuple[int, ...]
    symbol_keys: tuple[str, ...]
    edge_types: tuple[str, ...]
    directions: tuple[str, ...]
    round_index: int
    status: str
    scope: str = 'INDUCED'
    relation_ids: tuple[tuple, ...] = ()
    truncated: bool = False
    error: str | None = None
    observation_id: str = ''

    def __post_init__(self):
        if self.status not in {'NOT_QUERIED', 'COMPLETED', 'TIMEOUT', 'INDEX_UNAVAILABLE', 'FAILED', 'DEADLINE'}:
            raise ValueError('Invalid relation probe status')
        if not set(self.edge_types) <= EDGES or self.scope not in {'INDUCED', 'NEIGHBORS'}:
            raise ValueError('Invalid relation probe scope')


@dataclass(frozen=True, slots=True)
class ConfirmedPath:
    requirement_id: str
    need_index: int
    nodes: tuple[str, ...]
    relation_ids: tuple[tuple, ...]
    directions: tuple[str, ...]
    round_index: int


def metadata(value):
    return asdict(value)
