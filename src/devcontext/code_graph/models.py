from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from devcontext.models import Chunk, SearchResult

SYMBOL_KINDS = {"CLASS", "INTERFACE", "METHOD", "CONSTRUCTOR"}
EDGE_TYPES = {"EXTENDS", "IMPLEMENTS", "CALLS", "CONSTRUCTS", "OVERRIDES"}
RESOLUTION_KINDS = {"AST_EXACT", "SYMBOL_SOLVER_EXACT", "DERIVED_EXACT"}


class AnalysisContractError(ValueError):
    """Fatal invalid output; resolution gaps belong in diagnostics instead."""


@dataclass(frozen=True, slots=True)
class CodeSymbol:
    repository: str
    symbol_key: str
    symbol_kind: str
    simple_name: str
    qualified_name: str
    canonical_signature: str | None
    owner_symbol_key: str | None
    chunk_ref: int
    file_path: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class SymbolEdge:
    repository: str
    source_symbol_key: str
    target_symbol_key: str
    edge_type: str
    source_line: int
    source_column: int
    resolution_kind: str


@dataclass(slots=True)
class JavaAnalysisResult:
    chunks: list[Chunk]
    symbols: list[CodeSymbol]
    edges: list[SymbolEdge]
    diagnostics: dict[str, Any]

    def validate(self, repository: str) -> None:
        validate_snapshot(repository, self.chunks, self.symbols, self.edges)


def validate_snapshot(repository, chunks, symbols, edges) -> None:
    keys: dict[str, CodeSymbol] = {}
    refs: set[int] = set()
    if any(chunk.repository != repository for chunk in chunks):
        raise AnalysisContractError("Chunk repository differs from snapshot")
    for symbol in symbols:
        if symbol.repository != repository or symbol.symbol_kind not in SYMBOL_KINDS:
            raise AnalysisContractError("Invalid symbol repository or kind")
        if symbol.symbol_key in keys or symbol.chunk_ref in refs:
            raise AnalysisContractError("Duplicate symbol identity or chunk reference")
        if not 0 <= symbol.chunk_ref < len(chunks):
            raise AnalysisContractError("Symbol chunk reference is out of range")
        chunk = chunks[symbol.chunk_ref]
        if (chunk.source_type, chunk.chunk_type, chunk.file_path, chunk.start_line, chunk.end_line) != (
            "CODE", symbol.symbol_kind, symbol.file_path, symbol.start_line, symbol.end_line
        ):
            raise AnalysisContractError("Symbol chunk locator does not match")
        prefix = {"CLASS": "T:", "INTERFACE": "T:", "METHOD": "M:", "CONSTRUCTOR": "C:"}[symbol.symbol_kind]
        if not symbol.symbol_key.startswith(prefix):
            raise AnalysisContractError("Invalid symbol key prefix")
        keys[symbol.symbol_key] = symbol
        refs.add(symbol.chunk_ref)
    for symbol in symbols:
        if symbol.owner_symbol_key is not None:
            owner = keys.get(symbol.owner_symbol_key)
            if owner is None or owner.symbol_kind not in {"CLASS", "INTERFACE"}:
                raise AnalysisContractError("Invalid symbol owner")
        if symbol.symbol_kind in {"METHOD", "CONSTRUCTOR"} and symbol.owner_symbol_key is None:
            raise AnalysisContractError("Callable has no owner")
        if symbol.symbol_kind in {"CLASS", "INTERFACE"}:
            expected_key = "T:" + symbol.qualified_name
            if symbol.canonical_signature is not None:
                raise AnalysisContractError("Type symbol has a callable signature")
        else:
            owner = keys[symbol.owner_symbol_key]
            member = "<init>" if symbol.symbol_kind == "CONSTRUCTOR" else symbol.simple_name
            if symbol.qualified_name != owner.qualified_name + "#" + member:
                raise AnalysisContractError("Callable qualified name differs from owner")
            if not symbol.canonical_signature or not symbol.canonical_signature.startswith(member + "(") or not symbol.canonical_signature.endswith(")"):
                raise AnalysisContractError("Invalid canonical callable signature")
            expected_key = ("C:" if symbol.symbol_kind == "CONSTRUCTOR" else "M:") + owner.qualified_name + "#" + symbol.canonical_signature
        if symbol.symbol_key != expected_key:
            raise AnalysisContractError("Symbol key differs from canonical identity")
        seen = {symbol.symbol_key}
        current = symbol
        while current.owner_symbol_key is not None:
            if current.owner_symbol_key in seen:
                raise AnalysisContractError("Cyclic symbol ownership")
            seen.add(current.owner_symbol_key)
            next_owner = keys.get(current.owner_symbol_key)
            if next_owner is None or next_owner.symbol_kind not in {"CLASS", "INTERFACE"}:
                raise AnalysisContractError("Invalid ancestor owner")
            current = next_owner
    for edge in edges:
        if (edge.repository != repository or edge.edge_type not in EDGE_TYPES
                or edge.resolution_kind not in RESOLUTION_KINDS
                or edge.source_symbol_key not in keys or edge.target_symbol_key not in keys
                or edge.source_line < 1 or edge.source_column < 1):
            raise AnalysisContractError("Invalid edge or missing endpoint")
        source, target = keys[edge.source_symbol_key], keys[edge.target_symbol_key]
        valid = {
            "EXTENDS": source.symbol_kind == target.symbol_kind and source.symbol_kind in {"CLASS", "INTERFACE"},
            "IMPLEMENTS": source.symbol_kind == "CLASS" and target.symbol_kind == "INTERFACE",
            "CALLS": source.symbol_kind in {"METHOD", "CONSTRUCTOR"} and target.symbol_kind == "METHOD",
            "CONSTRUCTS": source.symbol_kind in {"METHOD", "CONSTRUCTOR"} and target.symbol_kind == "CONSTRUCTOR",
            "OVERRIDES": source.symbol_kind == target.symbol_kind == "METHOD",
        }[edge.edge_type]
        if not valid:
            raise AnalysisContractError("Edge endpoint kinds differ from relation")


@dataclass(slots=True)
class GraphExpansionTrace:
    action_id: str
    requirement_id: str
    round_index: int
    intent: str
    search_strategy: str | None = None
    anchors: list[str] = field(default_factory=list)
    exact_added_chunks: list[int] = field(default_factory=list)
    graph_added_chunks: list[int] = field(default_factory=list)
    paths: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    skip_reason: str | None = None
    error: str | None = None
    truncated: bool = False
    stale_anchor_count: int = 0
    duplicate_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class GraphExpansion:
    results: list[SearchResult]
    trace: GraphExpansionTrace
