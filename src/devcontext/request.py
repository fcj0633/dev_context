from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


ANSWER_DEPTHS = ("brief", "standard", "detailed")
TEACH_DEPTHS = ("brief", "standard", "detailed", "deep")
ANSWER_MODES = ("legacy", "explain", "teach")
# The two paths that predate the teaching pipeline. They share its depth
# vocabulary but not `deep`: a single-pass generator has no way to honour it, so
# accepting it would silently produce an ordinary answer under a deep label.
EXPLAIN_MODES = ("legacy", "explain")


def depths_for(answer_mode: str) -> tuple[str, ...]:
    return TEACH_DEPTHS if answer_mode == "teach" else ANSWER_DEPTHS


# Which evidence set decides whether anything was retrieved at all. The teach
# path builds its own views over the whole workspace, so for it an empty
# retrieval bundle means the bundle was too small, not that nothing was found.
# The older paths answer from that bundle, so for them it is still the measure.
EVIDENCE_SOURCE_BY_MODE = {
    "legacy": "bundle",
    "explain": "bundle",
    "teach": "workspace",
}


@dataclass(frozen=True, slots=True)
class AnswerOptions:
    """Explicit presentation preferences supplied by the caller.

    These values are deliberately kept outside the evidence plan: changing how
    much prose the user wants must not change which project facts are searched.
    """

    depth_override: str | None = None
    answer_mode: str = "legacy"

    def __post_init__(self) -> None:
        if self.answer_mode not in ANSWER_MODES:
            raise ValueError(
                "answer_mode must be one of " + ", ".join(ANSWER_MODES)
            )
        if self.depth_override is None:
            return
        allowed = depths_for(self.answer_mode)
        if self.depth_override not in allowed:
            raise ValueError(
                f"depth {self.depth_override!r} is not available for "
                f"answer_mode {self.answer_mode!r}; expected one of "
                + ", ".join(allowed)
            )

    @property
    def evidence_source(self) -> str:
        return EVIDENCE_SOURCE_BY_MODE.get(self.answer_mode, "bundle")

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
