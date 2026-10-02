from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from devcontext.context.budget import ModelCapabilities

if TYPE_CHECKING:
    from devcontext.explanation.models import ExplanationPlan


# Tokens a section is expected to need, by depth. These are planning weights, not
# ceilings: nothing refuses an answer for exceeding them, which is the point -
# the old 2200-5000 character window was the binding constraint on hard
# questions, not the model's window.
_TOKENS_PER_SECTION = {
    "brief": 400,
    "standard": 900,
    "detailed": 1_800,
    "deep": 2_800,
}
_COMPOSER_RESERVE_TOKENS = 2_000
# Across the first 64 live high-effort section/revision calls, the nearest-rank
# reasoning-token p95 was 4,135. Round it to the next 256-token bucket. This is
# completion-total headroom: visible targets remain separate and are never
# subtracted from provider usage.
WRITER_REASONING_RESERVE_TOKENS = 4_352
# Above this many sections a single pass stops being able to hold the whole
# argument, so the answer is written section by section and joined afterwards.
FAST_PATH_MAX_SECTIONS = 3


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
        section.target_tokens or _TOKENS_PER_SECTION[plan.answer_depth]
        for section in plan.sections
    )
    wanted = per_section + _COMPOSER_RESERVE_TOKENS

    multi_pass = plan.answer_depth in {"detailed", "deep"} or (
        section_count > FAST_PATH_MAX_SECTIONS
    )
    if not multi_pass:
        wanted = max(
            _TOKENS_PER_SECTION[plan.answer_depth], wanted // 2
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


def section_max_tokens(
    plan: ExplanationPlan,
    section: object,
    capabilities: ModelCapabilities,
    *,
    reasoning_reserve: int = WRITER_REASONING_RESERVE_TOKENS,
) -> int:
    target = getattr(section, "target_tokens", None) or _TOKENS_PER_SECTION[
        plan.answer_depth
    ]
    return _bounded_tokens(target, reasoning_reserve, capabilities)


def single_pass_max_tokens(
    plan: ExplanationPlan,
    capabilities: ModelCapabilities,
    *,
    reasoning_reserve: int = WRITER_REASONING_RESERVE_TOKENS,
) -> int:
    target = sum(
        section.target_tokens or _TOKENS_PER_SECTION[plan.answer_depth]
        for section in plan.sections
    )
    return _bounded_tokens(max(target, _TOKENS_PER_SECTION[plan.answer_depth]),
                           reasoning_reserve, capabilities)


def _bounded_tokens(
    visible_target: int,
    reasoning_reserve: int,
    capabilities: ModelCapabilities,
) -> int:
    wanted = max(4_096, int(visible_target * 1.5) + max(0, reasoning_reserve))
    rounded = ((wanted + 255) // 256) * 256
    return min(rounded, capabilities.max_output_tokens)
