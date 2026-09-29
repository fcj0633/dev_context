from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any


@dataclass(slots=True)
class Chunk:
    repository: str
    source_type: str
    chunk_type: str
    file_path: str
    content: str
    start_line: int | None = None
    end_line: int | None = None
    module: str | None = None
    package_name: str | None = None
    class_name: str | None = None
    symbol_name: str | None = None
    signature: str | None = None
    annotations: list[str] = field(default_factory=list)
    javadoc: str | None = None
    title: str | None = None
    heading_path: list[str] = field(default_factory=list)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = sha256(self.content.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Chunk":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value[key] for key in allowed if key in value})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def keyword_text(self) -> str:
        values = [
            self.symbol_name,
            self.class_name,
            self.signature,
            self.title,
            " / ".join(self.heading_path),
            self.file_path,
            self.javadoc,
            self.content,
        ]
        return "\n".join(value for value in values if value)

    def embedding_text(self) -> str:
        heading = " / ".join(self.heading_path)
        identity = self.signature or self.symbol_name or self.title or heading
        values = [identity, self.javadoc, self.content]
        return "\n\n".join(value.strip() for value in values if value and value.strip())


@dataclass(slots=True)
class SearchResult:
    id: int
    source_type: str
    chunk_type: str
    file_path: str
    content: str
    start_line: int | None
    end_line: int | None
    class_name: str | None
    symbol_name: str | None
    signature: str | None
    title: str | None
    score: float
    annotations: list[str] = field(default_factory=list)
    heading_path: list[str] = field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "SearchResult":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["content_preview"] = " ".join(self.content.split())[:300]
        del data["content"]
        return data


@dataclass(slots=True)
class SearchTimings:
    query_embedding_ms: float = 0.0
    keyword_sql_ms: float = 0.0
    vector_sql_ms: float = 0.0
    fusion_ms: float = 0.0
    total_ms: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class SearchExecution:
    results: list[SearchResult]
    timings: SearchTimings
    source_candidates: dict[str, list[SearchResult]] = field(default_factory=dict)


@dataclass(slots=True)
class Citation:
    label: str
    source_type: str
    file_path: str
    class_name: str | None = None
    symbol_name: str | None = None
    signature: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    heading_path: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ContextItem:
    citation: Citation
    content: str
    chunk_id: int
    chunk_type: str
    score: float
    retrieval_rank: int
    truncated: bool = False
    source_role: str = "UNKNOWN"
    temporal_status: str = "UNKNOWN"
    authority_priority: int = 50
    sub_question_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ContextBundle:
    query: str
    items: list[ContextItem]
    rendered_text: str
    total_chars: int
    max_chars: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AnswerResult:
    answer: str
    used_citations: list[str]
    invalid_citations: list[str] = field(default_factory=list)
    zero_valid_citation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
