"""Rank fusion for hybrid retrieval.

Keyword (full-text) and vector rankings use incomparable score scales, so they are combined by rank:
weighted reciprocal rank fusion, score = sum_s w_s / (k + rank_s). ``k`` and the weights are tuned on labelled
queries (see docs/rag-evaluation.md); the defaults are the tuned values. Fused scores order results; they are
not probabilities and are never shown as a screening score.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

Aggregation = Literal["max", "sum_top2"]


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]], *, k: int = 60, weights: Mapping[str, float] | None = None
) -> dict[str, float]:
    """Fused score per item id; each ranking lists ids best first (duplicates after the first are ignored)."""
    scores: dict[str, float] = defaultdict(float)
    for signal, ranking in rankings.items():
        weight = 1.0 if weights is None else weights.get(signal, 0.0)
        if weight <= 0:
            continue
        seen: set[str] = set()
        rank = 0
        for item in ranking:
            if item in seen:
                continue
            seen.add(item)
            rank += 1
            scores[item] += weight / (k + rank)
    return dict(scores)


@dataclass
class RankedCandidate:
    candidate_id: str
    score: float
    chunk_ids: list[str] = field(default_factory=list)  # best first


def aggregate_candidates(
    chunk_scores: Mapping[str, float],
    chunk_candidate: Mapping[str, str],
    *,
    method: Aggregation = "max",
) -> list[RankedCandidate]:
    """Candidate ranking from chunk scores: the best chunk ("max"), or the sum of the two best ("sum_top2")."""
    by_candidate: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for chunk_id, score in chunk_scores.items():
        by_candidate[chunk_candidate[chunk_id]].append((score, chunk_id))
    out = []
    for candidate_id, scored in by_candidate.items():
        scored.sort(key=lambda s: (-s[0], s[1]))
        value = scored[0][0] if method == "max" else sum(s for s, _ in scored[:2])
        out.append(RankedCandidate(candidate_id, value, [c for _, c in scored]))
    out.sort(key=lambda c: (-c.score, c.candidate_id))
    return out
