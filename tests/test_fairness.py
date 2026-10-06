from __future__ import annotations

import pytest

from engine.config import get_settings
from engine.extraction import extract_job_requirements
from eval.fairness import apply_variant, run_counterfactuals, variants
from eval.golden import load_golden
from eval.run_eval import FAIRNESS_BASES, FAIRNESS_MAX_DELTA, fairness_failures


@pytest.fixture(scope="module")
def setup():
    golden = load_golden()
    reqs = {
        job_id: extract_job_requirements(text, None).requirements.confirm(by="fairness-test")
        for job_id, text in golden.jobs.items()
    }
    bases = {f: golden.resume_path(f).read_text(encoding="utf-8") for f in FAIRNESS_BASES}
    return golden, reqs, bases


def test_variant_set_spans_regions_genders_and_pronouns() -> None:
    vs = variants("full")
    assert len(vs) == 28 and len({v.group for v in vs}) >= 12
    assert {v.gender for v in vs} == {"male", "female", "non-binary"}
    assert {v.subject for v in vs} == {"he", "she", "they"}


def test_apply_variant_injects_name_email_and_pronouns(setup) -> None:
    _, _, bases = setup
    v = next(v for v in variants() if v.gender == "female")
    text = apply_variant(bases["g01_ml_below.txt"], v)
    lines = text.splitlines()
    assert lines[0] == v.name
    assert "@example.com" in lines[1] and v.name.split()[-1].lower() in lines[1]
    assert "She is open to relocating for her next role." in text
    assert ", mentoring her team" in text


def test_ci_scope_is_six_diverse_variants() -> None:
    vs = variants("ci")
    assert len(vs) == 6
    assert {v.gender for v in vs} == {"male", "female", "non-binary"}
    assert len({v.group for v in vs}) >= 5


def test_counterfactual_scores_and_explanations_are_invariant(setup) -> None:
    golden, reqs, bases = setup
    cases = run_counterfactuals(reqs, bases, golden.reference_date, scope=get_settings().fairness_scope)
    assert len(cases) == 3  # FAIRNESS_SCOPE defaults to "ci"
    assert fairness_failures(cases) == []
    assert max(c.delta for c in cases) < FAIRNESS_MAX_DELTA


def test_harness_detects_substantive_differences(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: if a variant changes real content, the harness must flag it."""
    golden, reqs, bases = setup
    import eval.fairness as fairness

    original = fairness.apply_variant

    def biased(text: str, v):  # female variants silently lose a required skill
        out = original(text, v)
        return out.replace("PyTorch", "Pascal") if v.gender == "female" else out

    monkeypatch.setattr(fairness, "apply_variant", biased)
    cases = run_counterfactuals(
        {"ml_engineer": reqs["ml_engineer"]},
        {"g01_ml_below.txt": bases["g01_ml_below.txt"]},
        golden.reference_date,
        scope="ci",
    )
    [case] = cases
    assert case.delta >= FAIRNESS_MAX_DELTA or not case.explanation_consistent
    assert fairness_failures(cases)
