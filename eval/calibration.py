"""Calibrate "low" thresholds for similarity features from golden labels.

Similarity scales depend on the backend (TF-IDF cosines run far lower than embedding cosines), so a
fixed threshold would mislabel candidates. For each similarity feature we pick the threshold that best
separates weak candidates (label <= 1) from relevant ones (label >= 2), by balanced accuracy, and
store it per backend in ``engine/similarity_calibration.json`` for the explanation templates.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from engine.explain import CALIBRATION_PATH, SIMILARITY_FEATURES
from engine.scorer import GoldenExample


def best_threshold(values_labels: Sequence[tuple[float, int]]) -> dict | None:
    """Threshold t maximizing balanced accuracy of "value < t" as a predictor of label <= 1.

    Ties go to the lowest threshold (fewest "low" flags). None if either class is missing.
    """
    low = [v for v, lb in values_labels if lb <= 1]
    high = [v for v, lb in values_labels if lb >= 2]
    if not low or not high:
        return None
    values = sorted({v for v, _ in values_labels})
    candidates = [(a + b) / 2 for a, b in pairwise(values)] or values
    best = None
    for t in candidates:
        tpr = sum(v < t for v in low) / len(low)
        tnr = sum(v >= t for v in high) / len(high)
        score = (tpr + tnr) / 2
        if best is None or score > best[1] + 1e-12:
            best = (t, score)
    assert best is not None
    return {"threshold": round(best[0], 4), "balanced_accuracy": round(best[1], 4), "n": len(values_labels)}


def calibrate(examples: Sequence[GoldenExample]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for feature in SIMILARITY_FEATURES:
        pairs = [(ex.features[feature], ex.label) for ex in examples if ex.features.get(feature) is not None]
        result = best_threshold(pairs)  # type: ignore[arg-type]
        if result:
            out[feature] = result
    return out


def write_calibration(backend: str, results: dict[str, dict], path: Path = CALIBRATION_PATH) -> None:
    """Merge this backend's thresholds into the calibration file (other backends are kept)."""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"backends": {}}
    data["backends"][backend] = {
        "thresholds": {f: r["threshold"] for f, r in results.items()},
        "details": results,
        "calibrated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "eval/golden (python -m eval.run_eval --calibrate)",
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _balanced_accuracy(values_labels: Sequence[tuple[float, int]], threshold: float) -> float | None:
    low = [v for v, lb in values_labels if lb <= 1]
    high = [v for v, lb in values_labels if lb >= 2]
    if not low or not high:
        return None
    return (sum(v < threshold for v in low) / len(low) + sum(v >= threshold for v in high) / len(high)) / 2


def calibrate_leave_one_job_out(examples: Sequence[GoldenExample]) -> dict[str, dict]:
    """Per feature: threshold fitted on all other jobs, scored on the held-out job (mean over folds).

    Reported next to the in-sample balanced accuracy, which is optimistic because the threshold was
    chosen on the same labels.
    """
    jobs = sorted({ex.job_id for ex in examples})
    out: dict[str, dict] = {}
    for feature in SIMILARITY_FEATURES:
        pairs_by_job = {
            job: [
                (ex.features[feature], ex.label)
                for ex in examples
                if ex.job_id == job and ex.features.get(feature) is not None
            ]
            for job in jobs
        }
        held_out_scores: list[float] = []
        for job in jobs:
            train = [p for j, ps in pairs_by_job.items() if j != job for p in ps]
            fitted = best_threshold(train)  # type: ignore[arg-type]
            if fitted is None:
                continue
            score = _balanced_accuracy(pairs_by_job[job], fitted["threshold"])  # type: ignore[arg-type]
            if score is not None:
                held_out_scores.append(score)
        if held_out_scores:
            out[feature] = {
                "heldout_balanced_accuracy": round(sum(held_out_scores) / len(held_out_scores), 4),
                "folds": len(held_out_scores),
            }
    return out
