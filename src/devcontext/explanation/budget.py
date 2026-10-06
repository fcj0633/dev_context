from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from devcontext.context.budget import ModelCapabilities

if TYPE_CHECKING:
    from devcontext.explanation.models import ExplanationPlan


# Section weights reflect the planned understanding task, not a depth tier.
_COMPOSER_RESERVE_TOKENS = 2_000
# Above this many sections a single pass stops being able to hold the whole
# argument, so the answer is written section by section and joined afterwards.
FAST_PATH_MAX_SECTIONS = 1


@dataclass(frozen=True, slots=True)
class OutputBudget:
    max_output_tokens: int
    preferred_sections: int
    allow_multi_pass: bool
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "max_output_tokens": self.max_output_tokens,
            "preferred_sections": self.preferred_sections,
            "allow_multi_pass": self.allow_multi_pass,
            "reason": self.reason,
        }


def budget_for(
    plan: ExplanationPlan,
    capabilities: ModelCapabilities,
) -> OutputBudget:
    """Size the answer from the plan and the model, not from a fixed window."""
    section_count = len(plan.sections)
    per_section = sum(
        section.target_tokens or 1800
        for section in plan.sections
    )
    wanted = per_section + _COMPOSER_RESERVE_TOKENS

    multi_pass = section_count > FAST_PATH_MAX_SECTIONS
    if not multi_pass:
        wanted = max(
            1800, wanted // 2
        )

    if wanted <= capabilities.max_output_tokens:
        return OutputBudget(
            max_output_tokens=wanted,
            preferred_sections=section_count,
            allow_multi_pass=multi_pass,
            reason="sized from the explanation plan",
        )
    return OutputBudget(
        max_output_tokens=capabilities.max_output_tokens,
        preferred_sections=section_count,
        allow_multi_pass=multi_pass,
        reason="capped by the model's maximum output",
    )
