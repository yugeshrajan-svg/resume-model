"""Optional comparison: LambdaMART (LightGBM) vs logistic-regression/grid-search weights (fit_weights).

Evaluation only — production scoring stays a transparent weighted sum. Requires the ``ltr`` extra
(``pip install .[ltr]``). Uses leave-one-job-out cross-validation over the golden jobs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

import numpy as np

from engine.config import get_settings
from engine.features import FEATURE_NAMES
from engine.scorer import GoldenExample, fit_weights, mean_ndcg, ndcg_at_k, normalize_weights


def _matrix(examples: Sequence[GoldenExample]) -> np.ndarray:
    # LightGBM handles NaN natively, so N/A features stay missing rather than becoming 0.
    return np.array([[np.nan if ex.features[n] is None else ex.features[n] for n in FEATURE_NAMES] for ex in examples])


def compare_ltr(examples: Sequence[GoldenExample], k: int = 10) -> dict[str, float | int]:
    """Mean held-out NDCG@k for default weights, fitted weights and LambdaMART (leave-one-job-out)."""
    try:
        from lightgbm import LGBMRanker
    except ImportError as exc:
        raise ImportError("LambdaMART comparison needs LightGBM: pip install .[ltr]") from exc

    by_job: dict[str, list[GoldenExample]] = defaultdict(list)
    for ex in examples:
        by_job[ex.job_id].append(ex)
    if len(by_job) < 2:
        raise ValueError("Leave-one-job-out needs at least two golden jobs")

    default = normalize_weights(get_settings().score_weights)
    scores: dict[str, list[float]] = {"default_weights": [], "fit_weights": [], "lambdamart": []}
    for held_out, test in by_job.items():
        train = [ex for job, exs in by_job.items() if job != held_out for ex in exs]
        scores["default_weights"].append(mean_ndcg(test, default, k))
        scores["fit_weights"].append(mean_ndcg(test, fit_weights(train, baseline=default).weights, k))

        train_sorted = sorted(train, key=lambda ex: ex.job_id)
        groups = [
            sum(1 for ex in train_sorted if ex.job_id == job) for job in dict.fromkeys(ex.job_id for ex in train_sorted)
        ]
        ranker = LGBMRanker(
            objective="lambdarank",
            n_estimators=100,
            learning_rate=0.1,
            num_leaves=7,
            min_child_samples=2,
            random_state=0,
            verbose=-1,
        )
        ranker.fit(_matrix(train_sorted), [ex.label for ex in train_sorted], group=groups)
        predicted = ranker.predict(_matrix(test))
        order = sorted(range(len(test)), key=lambda i: (-predicted[i], test[i].candidate_id))
        scores["lambdamart"].append(ndcg_at_k([test[i].label for i in order], k) or 0.0)

    return {
        **{name: round(sum(vals) / len(vals), 4) for name, vals in scores.items()},
        "folds": len(by_job),
        "examples": len(examples),
    }
