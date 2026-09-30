from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EstimateComparison:
    estimated: int
    actual: int
    error_ratio: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "estimated_input_tokens": self.estimated,
            "actual_input_tokens": self.actual,
            "error_ratio": self.error_ratio,
        }


class TokenEstimator(Protocol):
    def estimate(self, text: str) -> int: ...


def compare_estimate(estimated: int, actual: int) -> EstimateComparison:
    """Pair an estimate with the usage the API actually reported.

    The point of keeping both is calibration. A rough estimate is fine as long as
    the drift is measured rather than assumed away.
    """
    if actual <= 0:
        raise ValueError("actual token count must be positive")
    return EstimateComparison(
        estimated=estimated,
        actual=actual,
        error_ratio=round((estimated - actual) / actual, 4),
    )


@dataclass(frozen=True, slots=True)
class HeuristicTokenEstimator:
    """A dependency-free estimate that errs high rather than low.

    ``pyproject.toml`` has no tokenizer, so this deliberately over-counts: CJK is
    charged one token per character because that is roughly what these models do,
    and non-CJK one token per ``latin_chars_per_token``. For a Chinese corpus the
    effective assumption is "chars ~= tokens", which is why the same character
    count in English and Chinese must not produce the same budget.
    """

    latin_chars_per_token: float = 4.0
    # Used to turn a token budget back into a character budget. 1.0 is the
    # pessimistic end of the range for this corpus, so a view built from it can
    # only come in under budget.
    conservative_chars_per_token: float = 1.0

    def estimate(self, text: str) -> int:
        if not text:
            return 0
        cjk = sum(1 for char in text if _is_cjk(char))
        other = len(text) - cjk
        return cjk + int(-(-other // self.latin_chars_per_token))

    def chars_for_tokens(self, tokens: int) -> int:
        if tokens < 1:
            raise ValueError("token budget must be positive")
        return int(tokens * self.conservative_chars_per_token)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3000 <= code <= 0x303F      # CJK punctuation
        or 0x4E00 <= code <= 0x9FFF   # CJK unified ideographs
        or 0x3400 <= code <= 0x4DBF   # extension A
        or 0xFF00 <= code <= 0xFFEF   # fullwidth forms
        or 0x20000 <= code <= 0x2FA1F  # extensions B-F
    )
