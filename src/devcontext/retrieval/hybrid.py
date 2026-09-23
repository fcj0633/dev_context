from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from devcontext.models import SearchResult


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchResult]],
    *,
    k: int = 60,
    top_k: int = 10,
) -> list[SearchResult]:
    scores: dict[int, float] = {}
    results: dict[int, SearchResult] = {}
    for ranking in rankings:
        for rank, result in enumerate(ranking, start=1):
            scores[result.id] = scores.get(result.id, 0.0) + 1.0 / (k + rank)
            results.setdefault(result.id, result)
    ordered_ids = sorted(scores, key=lambda item: (-scores[item], item))[:top_k]
    return [replace(results[item], score=scores[item]) for item in ordered_ids]
