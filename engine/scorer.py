"""Weighted scoring (0-100) and weight fitting against golden labels."""

from __future__ import annotations

import itertools
import logging
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence

import numpy as np
from pydantic import BaseModel, Field
from sklearn.linear_model import LogisticRegression

from engine.config import ConfigError, Settings, get_settings
from engine.features import FEATURE_NAMES
from engine.logging_setup import log_event
from engine.schemas import FeatureResult, JobRequirements, ScoreBreakdown

logger = logging.getLogger("engine.scorer")


# ---------------------------------------------------------------------------
# Weights & scoring
# ---------------------------------------------------------------------------


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Validate (known features, non-negative, positive sum) and scale to sum 1. Missing features get 0."""
    unknown = set(weights) - set(FEATURE_NAMES)
    if unknown:
        raise ConfigError(f"Unknown feature(s) in weights: {sorted(unknown)}; valid: {list(FEATURE_NAMES)}")
    if any(not math.isfinite(w) or w < 0 for w in weights.values()):
        raise ConfigError(f"Weights must be finite and non-negative: {weights}")
    total = sum(weights.values())
    if total <= 0:
        raise ConfigError("Weights must not all be zero")
    return {name: weights.get(name, 0.0) / total for name in FEATURE_NAMES}


def resolve_weights(req: JobRequirements, settings: Settings | None = None) -> dict[str, float]:
    """The job's score_weights if set (a complete override), else the configured defaults."""
    settings = settings or get_settings()
    return normalize_weights(req.score_weights if req.score_weights is not None else settings.score_weights)


def effective_weights(values: dict[str, float | None], weights: dict[str, float]) -> dict[str, float]:
    """Renormalize ``weights`` over applicable features (value not None); N/A features get 0."""
    total = sum(weights[n] for n in FEATURE_NAMES if values[n] is not None)
    if total <= 0:
        raise ConfigError("None of the applicable features has a positive weight; cannot score")
    return {n: (weights[n] / total if values[n] is not None else 0.0) for n in FEATURE_NAMES}


def weighted_score(values: dict[str, float | None], weights: dict[str, float]) -> float:
    """0-100 score from feature values (None = not applicable) and configured weights."""
    eff = effective_weights(values, weights)
    return sum(eff[n] * (values[n] or 0.0) for n in FEATURE_NAMES) * 100


def score_features(features: Sequence[FeatureResult], weights: dict[str, float]) -> ScoreBreakdown:
    """Weighted sum over applicable features, with weights renormalized to sum 1 across them."""
    values: dict[str, float | None] = {f.name: (f.value if f.applicable else None) for f in features}
    missing = set(FEATURE_NAMES) - set(values)
    if missing:
        raise ValueError(f"Missing feature values: {sorted(missing)}")
    eff = effective_weights(values, weights)
    contributions = {n: round(eff[n] * (values[n] or 0.0) * 100, 2) for n in FEATURE_NAMES}
    score = round(weighted_score(values, weights), 2)
    return ScoreBreakdown(
        score=min(max(score, 0.0), 100.0),
        configured_weights=weights,
        weights={n: round(w, 4) for n, w in eff.items()},
        not_applicable=[n for n in FEATURE_NAMES if values[n] is None],
        contributions=contributions,
    )


# ---------------------------------------------------------------------------
# Ranking metric
# ---------------------------------------------------------------------------


def dcg_at_k(labels: Sequence[float], k: int) -> float:
    return sum((2**rel - 1) / math.log2(i + 2) for i, rel in enumerate(labels[:k]))


def ndcg_at_k(labels_in_ranked_order: Sequence[float], k: int = 10) -> float | None:
    """NDCG@k with graded gains (2^label - 1). None if no candidate has a positive label."""
    ideal = dcg_at_k(sorted(labels_in_ranked_order, reverse=True), k)
    if ideal == 0:
        return None
    return dcg_at_k(labels_in_ranked_order, k) / ideal


# ---------------------------------------------------------------------------
# Weight fitting
# ---------------------------------------------------------------------------


class GoldenExample(BaseModel):
    job_id: str
    candidate_id: str
    features: dict[str, float | None]  # feature name -> value in [0, 1], None if not applicable
    label: int = Field(ge=0, le=3)


class WeightFitResult(BaseModel):
    weights: dict[str, float]
    ndcg_at_10: float
    baseline_weights: dict[str, float]
    baseline_ndcg_at_10: float
    method: str
    n_jobs: int
    n_examples: int
    candidates_evaluated: int


def _fit_score(values: dict[str, float | None], weights: dict[str, float]) -> float:
    # During the weight search a grid point may weight only features that are N/A for this candidate.
    try:
        return weighted_score(values, weights)
    except ConfigError:
        return 0.0


def mean_ndcg(examples: Sequence[GoldenExample], weights: dict[str, float], k: int = 10) -> float:
    by_job: dict[str, list[GoldenExample]] = defaultdict(list)
    for ex in examples:
        by_job[ex.job_id].append(ex)
    scores: list[float] = []
    for job_examples in by_job.values():
        ranked = sorted(
            job_examples,
            key=lambda ex: (-_fit_score(ex.features, weights), ex.candidate_id),
        )
        value = ndcg_at_k([ex.label for ex in ranked], k)
        if value is not None:
            scores.append(value)
    return sum(scores) / len(scores) if scores else 0.0


def _simplex_grid(step: float) -> Iterable[dict[str, float]]:
    n = round(1 / step)
    for cuts in itertools.combinations(range(n + len(FEATURE_NAMES) - 1), len(FEATURE_NAMES) - 1):
        parts = [b - a - 1 for a, b in zip((-1, *cuts), (*cuts, n + len(FEATURE_NAMES) - 1), strict=True)]
        yield {name: p / n for name, p in zip(FEATURE_NAMES, parts, strict=True)}


def _logistic_seed(examples: Sequence[GoldenExample]) -> dict[str, float] | None:
    """Logistic regression (label >= 2 vs < 2) coefficients clipped to >= 0 and normalized."""
    y = np.array([int(ex.label >= 2) for ex in examples])
    if len(set(y)) < 2:
        return None
    x = np.array([[ex.features[n] or 0.0 for n in FEATURE_NAMES] for ex in examples])
    coef = LogisticRegression(max_iter=1000).fit(x, y).coef_[0]
    clipped = np.clip(coef, 0, None)
    if clipped.sum() <= 0:
        return None
    return {n: float(c / clipped.sum()) for n, c in zip(FEATURE_NAMES, clipped, strict=True)}


def fit_weights(
    golden: Sequence[GoldenExample],
    baseline: dict[str, float] | None = None,
    step: float = 0.1,
    k: int = 10,
) -> WeightFitResult:
    """Learn non-negative weights (sum 1) that maximize mean NDCG@k over the golden jobs.

    Candidates: a logistic-regression seed plus every point on a simplex grid with ``step``.
    Ties are broken toward the baseline (current) weights, so the result is deterministic and
    only moves away from the current weights when that improves NDCG.
    """
    if not golden:
        raise ValueError("fit_weights needs at least one golden example")
    for ex in golden:
        if set(ex.features) != set(FEATURE_NAMES):
            raise ValueError(f"Example {ex.job_id}/{ex.candidate_id} must have exactly the features {FEATURE_NAMES}")
    if not 0 < step <= 0.5 or abs(round(1 / step) * step - 1) > 1e-9:
        raise ValueError("step must divide 1 evenly, e.g. 0.05, 0.1, 0.2")

    baseline = normalize_weights(baseline or get_settings().score_weights)
    baseline_ndcg = mean_ndcg(golden, baseline, k)

    candidates: list[tuple[str, dict[str, float]]] = [("baseline", baseline)]
    if (seed := _logistic_seed(golden)) is not None:
        candidates.append(("logistic_regression", seed))
    candidates.extend(("grid_search", w) for w in _simplex_grid(step))

    def distance(w: dict[str, float]) -> float:
        return sum(abs(w[n] - baseline[n]) for n in FEATURE_NAMES)

    best_method, best_weights, best_ndcg = "baseline", baseline, baseline_ndcg
    for method, weights in candidates:
        value = mean_ndcg(golden, weights, k)
        if value > best_ndcg + 1e-12 or (
            abs(value - best_ndcg) <= 1e-12 and distance(weights) < distance(best_weights)
        ):
            best_method, best_weights, best_ndcg = method, weights, value

    result = WeightFitResult(
        weights={n: round(w, 4) for n, w in best_weights.items()},
        ndcg_at_10=round(best_ndcg, 4),
        baseline_weights={n: round(w, 4) for n, w in baseline.items()},
        baseline_ndcg_at_10=round(baseline_ndcg, 4),
        method=best_method,
        n_jobs=len({ex.job_id for ex in golden}),
        n_examples=len(golden),
        candidates_evaluated=len(candidates),
    )
    log_event(logger, "weights_fitted", **result.model_dump(exclude={"weights", "baseline_weights"}))
    return result
