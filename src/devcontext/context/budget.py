from __future__ import annotations

from dataclasses import dataclass

from devcontext.context.estimator import TokenEstimator


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    context_window: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.context_window < 1:
            raise ValueError("context_window must be positive")
        if not 0 < self.max_output_tokens <= self.context_window:
            raise ValueError("max_output_tokens must be within the context window")


# Used when nothing is configured for a model. Deliberately small: guessing too
# high makes a prompt silently overflow, guessing low only wastes some room.
FALLBACK_CAPABILITIES = ModelCapabilities(context_window=32_768, max_output_tokens=8_192)


def capabilities_for(
    *,
    configured: ModelCapabilities | None = None,
    reported: ModelCapabilities | None = None,
) -> ModelCapabilities:
    """Resolve a model's limits: what the API reported, else config, else fallback.

    Kept as a function rather than a constant so a model's real limits come from
    its metadata or configuration instead of being hardcoded in stage code.
    """
    return reported or configured or FALLBACK_CAPABILITIES


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    context_budget_tokens: int
    output_reserve_tokens: int
    overflow_tokens: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "context_budget_tokens": self.context_budget_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
            "overflow_tokens": self.overflow_tokens,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class TokenBudgetPolicy:
    """Splits one model's context window between prompt and response, in tokens.

    Replaces the character budgets the retrieval stages used to carry. A character
    count is not a property of a model, so a prompt that fits under a 28,000
    character ceiling can still overflow a small window, and a large window gains
    nothing from a ceiling that was never tied to it.
    """

    capabilities: ModelCapabilities
    safety_margin_tokens: int = 1_024

    def __post_init__(self) -> None:
        if self.safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens must not be negative")

    def decide(
        self,
        *,
        fixed_tokens: int,
        requested_output_tokens: int,
    ) -> BudgetDecision:
        """Give the response its share first; the prompt gets what is left.

        ``fixed_tokens`` is everything that cannot be trimmed: system prompt,
        question, the plan itself. If even that does not fit, the answer is no.
        """
        if fixed_tokens < 0 or requested_output_tokens < 0:
            raise ValueError("token counts must not be negative")

        output_reserve = min(
            requested_output_tokens, self.capabilities.max_output_tokens
        )
        available = (
            self.capabilities.context_window
            - fixed_tokens
            - output_reserve
            - self.safety_margin_tokens
        )
        if available < 0:
            return BudgetDecision(
                allowed=False,
                context_budget_tokens=0,
                output_reserve_tokens=output_reserve,
                overflow_tokens=-available,
                reason=(
                    "fixed prompt and reserved output do not fit the context window"
                ),
            )
        return BudgetDecision(
            allowed=True,
            context_budget_tokens=available,
            output_reserve_tokens=output_reserve,
            overflow_tokens=0,
            reason="ok",
        )

    def context_chars_for(
        self, decision: BudgetDecision, estimator: TokenEstimator
    ) -> int:
        chars_for_tokens = getattr(estimator, "chars_for_tokens", None)
        if chars_for_tokens is None:
            raise TypeError("estimator must be able to turn tokens into characters")
        return chars_for_tokens(decision.context_budget_tokens)
