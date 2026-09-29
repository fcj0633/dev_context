from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ANSWER_DEPTHS = ("brief", "standard", "detailed")
ANSWER_MODES = ("legacy", "explain")


@dataclass(frozen=True, slots=True)
class AnswerOptions:
    """Explicit presentation preferences supplied by the caller.

    These values are deliberately kept outside the evidence plan: changing how
    much prose the user wants must not change which project facts are searched.
    """

    depth_override: str | None = None
    answer_mode: str = "legacy"

    def __post_init__(self) -> None:
        if self.depth_override is not None and self.depth_override not in ANSWER_DEPTHS:
            raise ValueError("depth_override must be brief, standard, or detailed")
        if self.answer_mode not in ANSWER_MODES:
            raise ValueError("answer_mode must be legacy or explain")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class UserRequest:
    """Validated request DTO; it performs no semantic interpretation."""

    original_query: str
    answer_options: AnswerOptions = AnswerOptions()
    context_budget_override: int | None = None

    def __post_init__(self) -> None:
        if not self.original_query.strip():
            raise ValueError("original_query must not be empty")
        if (
            self.context_budget_override is not None
            and self.context_budget_override < 1
        ):
            raise ValueError("context_budget_override must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "answer_options": self.answer_options.to_dict(),
            "context_budget_override": self.context_budget_override,
        }
