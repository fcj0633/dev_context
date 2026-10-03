from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True, frozen=True)
class LLMMessage:
    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMClient(Protocol):
    def generate(self, messages: Sequence[LLMMessage]) -> str:
        ...


@dataclass(slots=True, frozen=True)
class StreamEvent:
    type: str
    text: str = ""
    usage: dict[str, int] | None = None
    finish_reason: str | None = None
    retryable: bool = True


class StreamingLLMClient(Protocol):
    def generate_stream(self, messages: Sequence[LLMMessage]) -> Iterator[StreamEvent]:
        ...
