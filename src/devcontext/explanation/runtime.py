from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeachingRuntimeOptions:
    generation_mode: str = "multi_pass"

    def __post_init__(self):
        if self.generation_mode not in {"multi_pass", "single_stream"}:
            raise ValueError("invalid teaching generation mode")
