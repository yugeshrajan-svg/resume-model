from __future__ import annotations

import math
import random

import pytest

from engine.config import ConfigError, get_settings
from engine.features import FEATURE_NAMES
from engine.schemas import FeatureResult, JobRequirements
from engine.scorer import (
    GoldenExample,
    fit_weights,
    mean_ndcg,
    ndcg_at_k,
    normalize_weights,
    resolve_weights,
    score_features,
)


def _features(**values: float) -> list[FeatureResult]:
    return [FeatureResult(name=n, value=values.get(n, 0.0), detail="") for n in FEATURE_NAMES]


def _req(**overrides) -> JobRequirements:
    base = dict(
        role="Engineer", seniority=None, experience_min_years=None, required_skills=[],
        preferred_skills=[], locations=[], max_notice_period_days=None,
    )  # fmt: skip
    base.update(overrides)
    return JobRequirements(**base)


# ---------------------------------------------------------------------------
# Weights & scoring
# ---------------------------------------------------------------------------


def test_default_weights_sum_to_one() -> None:
    weights = resolve_weights(_req())
    assert math.isclose(sum(weights.values()), 1.0)
    assert set(weights) == set(FEATURE_NAMES)


def test_weights_are_normalized_and_missing_features_get_zero() -> None:
    weights = normalize_weights({"required_skill_coverage": 3, "semantic_relevance": 1})
    assert weights["required_skill_coverage"] == 0.75 and weights["semantic_relevance"] == 0.25
    assert weights["seniority_fit"] == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        {"made_up": 1.0},
        {"seniority_fit": -0.1, "semantic_relevance": 1},
        {"seniority_fit": 0.0},
        {"seniority_fit": math.inf},
    ],
)
def test_invalid_weights_fail_loudly(bad: dict[str, float]) -> None:
    with pytest.raises(ConfigError):
        normalize_weights(bad)


def test_per_job_override_replaces_config_weights() -> None:
    weights = resolve_weights(_req(score_weights={"relevant_experience": 1.0}))
    assert weights["relevant_experience"] == 1.0
    assert sum(weights.values()) == 1.0


def test_config_weights_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCORE_WEIGHTS", '{"seniority_fit": 1, "semantic_relevance": 1}')
    get_settings.cache_clear()
    assert resolve_weights(_req())["seniority_fit"] == 0.5


def test_score_range_and_contributions() -> None:
    weights = resolve_weights(_req())
    assert score_features(_features(**dict.fromkeys(FEATURE_NAMES, 1.0)), weights).score == 100.0
    assert score_features(_features(), weights).score == 0.0
    b = score_features(_features(required_skill_coverage=0.5, seniority_fit=1.0), weights)
    assert b.score == pytest.approx(0.30 * 50 + 0.10 * 100)
    assert sum(b.contributions.values()) == pytest.approx(b.score)


def test_score_requires_every_feature() -> None:
    with pytest.raises(ValueError, match="Missing feature"):
        score_features(_features()[:-1], resolve_weights(_req()))


# ---------------------------------------------------------------------------
# NDCG
# ---------------------------------------------------------------------------


def test_ndcg_perfect_and_reversed() -> None:
    assert ndcg_at_k([3, 2, 1, 0]) == 1.0
    reversed_ = ndcg_at_k([0, 1, 2, 3])
    assert reversed_ is not None and reversed_ < 0.6


def test_ndcg_known_value() -> None:
    # DCG = (2^1-1)/log2(2) + (2^3-1)/log2(3) ; IDCG = 7/1 + 1/log2(3)
    expected = (1 + 7 / math.log2(3)) / (7 + 1 / math.log2(3))
    assert ndcg_at_k([1, 3]) == pytest.approx(expected)


def test_ndcg_cutoff_and_no_relevant() -> None:
    assert ndcg_at_k([0] * 10 + [3], k=10) == 0.0
    assert ndcg_at_k([0, 0, 0]) is None


# ---------------------------------------------------------------------------
# Weight fitting
# ---------------------------------------------------------------------------


def _golden(seed: int = 7) -> list[GoldenExample]:
    """Labels driven by relevant_experience; other features are noise."""
    rng = random.Random(seed)
    examples = []
    for job in range(4):
        for cand in range(12):
            feats = {n: rng.random() for n in FEATURE_NAMES}
            label = min(3, int(feats["relevant_experience"] * 4))
            examples.append(GoldenExample(job_id=f"j{job}", candidate_id=f"c{cand}", features=feats, label=label))
    return examples


def test_fit_weights_finds_the_informative_feature() -> None:
    golden = _golden()
    result = fit_weights(golden)
    assert math.isclose(sum(result.weights.values()), 1.0, abs_tol=1e-3)
    assert all(w >= 0 for w in result.weights.values())
    assert max(result.weights, key=result.weights.get) == "relevant_experience"
    assert result.ndcg_at_10 >= result.baseline_ndcg_at_10
    assert result.ndcg_at_10 == pytest.approx(1.0)
    assert result.n_jobs == 4 and result.n_examples == 48
    assert result.candidates_evaluated > 3000  # baseline + LR seed + simplex grid


def test_fit_weights_is_deterministic_and_never_worse_than_baseline() -> None:
    golden = _golden(seed=11)
    a, b = fit_weights(golden), fit_weights(golden)
    assert a == b
    assert mean_ndcg(golden, a.weights) >= mean_ndcg(golden, a.baseline_weights) - 1e-9


def test_fit_weights_keeps_baseline_when_nothing_is_better() -> None:
    # All features identical across candidates: every weighting ties, so the baseline is kept.
    golden = [
        GoldenExample(job_id="j", candidate_id=f"c{i}", features=dict.fromkeys(FEATURE_NAMES, 0.5), label=i % 4)
        for i in range(8)
    ]
    result = fit_weights(golden)
    assert result.method == "baseline" and result.weights == result.baseline_weights


def test_fit_weights_validates_input() -> None:
    with pytest.raises(ValueError):
        fit_weights([])
    with pytest.raises(ValueError, match="exactly the features"):
        fit_weights([GoldenExample(job_id="j", candidate_id="c", features={"seniority_fit": 1.0}, label=1)])
    with pytest.raises(ValueError, match="step"):
        fit_weights(_golden(), step=0.3)


# ---------------------------------------------------------------------------
# Not-applicable features
# ---------------------------------------------------------------------------


def _with_na(na: set[str], **values: float) -> list[FeatureResult]:
    return [
        FeatureResult(name=n, applicable=n not in na, value=0.0 if n in na else values.get(n, 0.0), detail="")
        for n in FEATURE_NAMES
    ]


def test_weights_renormalize_over_applicable_features() -> None:
    weights = resolve_weights(_req())
    features = _with_na({"project_relevance", "preferred_skill_coverage"}, **dict.fromkeys(FEATURE_NAMES, 1.0))
    b = score_features(features, weights)
    assert b.score == 100.0  # a perfect candidate is not penalized for N/A features
    assert b.not_applicable == ["project_relevance", "preferred_skill_coverage"]
    assert b.weights["project_relevance"] == 0.0
    assert sum(b.weights.values()) == pytest.approx(1.0)
    assert b.weights["required_skill_coverage"] == pytest.approx(0.30 / 0.80)
    assert b.configured_weights["project_relevance"] == pytest.approx(0.10)


def test_na_scoring_matches_manual_renormalization() -> None:
    weights = resolve_weights(_req())
    b = score_features(_with_na({"project_relevance"}, required_skill_coverage=0.5, seniority_fit=1.0), weights)
    assert b.score == pytest.approx((0.30 * 0.5 + 0.10 * 1.0) / 0.90 * 100, abs=0.01)


def test_all_weight_on_na_features_fails_loudly() -> None:
    weights = normalize_weights({"project_relevance": 1.0})
    with pytest.raises(ConfigError):
        score_features(_with_na({"project_relevance"}), weights)


def test_fit_weights_accepts_na_features() -> None:
    golden = _golden()
    for ex in golden[::3]:
        ex.features["project_relevance"] = None
    result = fit_weights(golden)
    assert result.ndcg_at_10 >= result.baseline_ndcg_at_10
