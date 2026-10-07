from __future__ import annotations

import pytest

from engine.config import get_settings
from engine.knockout import evaluate_knockouts
from engine.schemas import CandidateSkill, JobRequirements, NormalizedCandidate, RequirementSkill


def _req(**overrides) -> JobRequirements:
    base = dict(
        role="Senior ML Engineer",
        seniority="senior",
        experience_min_years=5,
        required_skills=[
            RequirementSkill(name="Python", evidence=None),
            RequirementSkill(name="PyTorch", evidence=None),
            RequirementSkill(name="Kubernetes", evidence=None),
            RequirementSkill(name="Azure", evidence=None, alternatives=["AWS"]),
        ],
        preferred_skills=[],
        locations=["Bengaluru, India"],
        max_notice_period_days=60,
    )
    base.update(overrides)
    return JobRequirements(**base)


def _cand(
    skills: tuple[str, ...] = ("Python", "PyTorch", "Kubernetes", "Azure"),
    months: int = 72,
    location: str | None = "Bengaluru, India",
    notice: int | None = 30,
    undated: int = 0,
) -> NormalizedCandidate:
    return NormalizedCandidate(
        skills=[CandidateSkill(canonical=s, raw=s, evidence=s, in_ontology=True, last_used=None) for s in skills],
        jobs=[],
        projects=[],
        total_experience_months=months,
        undated_jobs=undated,
        seniority=4,
        location=location,
        notice_period_days=notice,
        reference_month="2026-10",
    )


def test_fully_matching_candidate_is_eligible() -> None:
    result = evaluate_knockouts(_req(), _cand())
    assert result.eligible and result.reasons == [] and result.warnings == []


# --- required skills -------------------------------------------------------


def test_default_threshold_is_ceil_80_percent() -> None:
    six = [
        RequirementSkill(name=n, evidence=None) for n in ("Python", "PyTorch", "Kubernetes", "SQL", "Docker", "Azure")
    ]
    req = _req(required_skills=six)  # ceil(0.8 * 6) = 5
    assert evaluate_knockouts(req, _cand(skills=("Python", "PyTorch", "Kubernetes", "SQL", "Docker"))).eligible
    failing = evaluate_knockouts(req, _cand(skills=("Python", "PyTorch", "Kubernetes", "SQL")))
    assert not failing.eligible and "needs 5" in failing.reasons[0].detail


def test_missing_required_skills_reported_even_when_eligible() -> None:
    six = [
        RequirementSkill(name=n, evidence=None) for n in ("Python", "PyTorch", "Kubernetes", "SQL", "Docker", "Azure")
    ]
    result = evaluate_knockouts(
        _req(required_skills=six), _cand(skills=("Python", "PyTorch", "Kubernetes", "SQL", "Docker"))
    )
    assert result.eligible and result.missing_required_skills == ["Azure"]


def test_all_mode_per_job() -> None:
    result = evaluate_knockouts(_req(required_skills_min_match="all"), _cand(skills=("Python", "PyTorch", "Azure")))
    assert not result.eligible and "needs 4" in result.reasons[0].detail


def test_missing_required_skill_with_four_required() -> None:
    # ceil(0.8 * 4) = 4: with four required skills the default still needs all of them.
    result = evaluate_knockouts(_req(), _cand(skills=("Python", "PyTorch", "Azure")))
    assert not result.eligible
    assert [r.rule for r in result.reasons] == ["required_skills"]
    assert result.missing_required_skills == ["Kubernetes"]
    assert "3 of 4" in result.reasons[0].detail


def test_alternative_satisfies_requirement() -> None:
    result = evaluate_knockouts(_req(), _cand(skills=("Python", "PyTorch", "Kubernetes", "AWS")))
    assert result.eligible


def test_missing_alternative_group_reported_with_options() -> None:
    result = evaluate_knockouts(_req(), _cand(skills=("Python", "PyTorch", "Kubernetes")))
    assert result.missing_required_skills == ["Azure or AWS"]


def test_alias_in_candidate_skills_is_canonicalized() -> None:
    result = evaluate_knockouts(_req(), _cand(skills=("python", "Pytorch", "k8s", "azure")))
    assert result.eligible


def test_n_of_m_per_job_override() -> None:
    req = _req(required_skills_min_match=3)
    assert evaluate_knockouts(req, _cand(skills=("Python", "PyTorch", "AWS"))).eligible
    failing = evaluate_knockouts(req, _cand(skills=("Python", "AWS")))
    assert not failing.eligible and "needs 3" in failing.reasons[0].detail


def test_invalid_min_match_values() -> None:
    with pytest.raises(ValueError):
        _req(required_skills_min_match=0)
    with pytest.raises(ValueError):
        _req(required_skills_min_match="most")


def test_n_of_m_via_configured_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOCKOUT_REQUIRED_SKILL_RATIO", "0.75")  # ceil(0.75 * 4) = 3
    get_settings.cache_clear()
    assert evaluate_knockouts(_req(), _cand(skills=("Python", "PyTorch", "AWS"))).eligible
    assert not evaluate_knockouts(_req(), _cand(skills=("Python", "AWS"))).eligible


def test_job_override_wins_over_configured_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOCKOUT_REQUIRED_SKILL_RATIO", "0.25")
    get_settings.cache_clear()
    assert not evaluate_knockouts(_req(required_skills_min_match=4), _cand(skills=("Python",))).eligible


def test_no_required_skills_means_no_skill_knockout() -> None:
    assert evaluate_knockouts(_req(required_skills=[]), _cand(skills=())).eligible


# --- experience ------------------------------------------------------------


def test_experience_below_minimum() -> None:
    result = evaluate_knockouts(_req(), _cand(months=59))
    assert [r.rule for r in result.reasons] == ["experience_min"]
    assert "4.9 years" in result.reasons[0].detail


def test_experience_exactly_minimum_passes() -> None:
    assert evaluate_knockouts(_req(), _cand(months=60)).eligible


def test_experience_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KNOCKOUT_EXPERIENCE_TOLERANCE_MONTHS", "6")
    get_settings.cache_clear()
    assert evaluate_knockouts(_req(), _cand(months=54)).eligible
    assert not evaluate_knockouts(_req(), _cand(months=53)).eligible


def test_undated_jobs_warn_when_experience_fails() -> None:
    result = evaluate_knockouts(_req(), _cand(months=24, undated=2))
    assert not result.eligible
    assert any("2 job(s) without parseable dates" in w for w in result.warnings)


def test_no_minimum_experience_means_no_experience_knockout() -> None:
    assert evaluate_knockouts(_req(experience_min_years=None), _cand(months=0)).eligible


# --- location --------------------------------------------------------------


def test_location_mismatch() -> None:
    result = evaluate_knockouts(_req(), _cand(location="Lyon, France"))
    assert [r.rule for r in result.reasons] == ["location"]


def test_remote_allowed_accepts_any_location() -> None:
    assert evaluate_knockouts(_req(locations=["Bengaluru, India", "Remote"]), _cand(location="Lyon, France")).eligible


def test_missing_location_is_a_warning_not_a_knockout() -> None:
    result = evaluate_knockouts(_req(), _cand(location=None))
    assert result.eligible and result.warnings == ["location not stated; location rule not applied"]


# --- notice period ---------------------------------------------------------


def test_notice_period_too_long() -> None:
    result = evaluate_knockouts(_req(), _cand(notice=90))
    assert [r.rule for r in result.reasons] == ["notice_period"]
    assert "90 days exceeds maximum 60" in result.reasons[0].detail


def test_notice_period_at_limit_passes() -> None:
    assert evaluate_knockouts(_req(), _cand(notice=60)).eligible


def test_missing_notice_period_is_a_warning() -> None:
    result = evaluate_knockouts(_req(), _cand(notice=None))
    assert result.eligible and "notice period not stated" in result.warnings[0]


# --- combined --------------------------------------------------------------


def test_all_failing_rules_are_reported_together() -> None:
    result = evaluate_knockouts(_req(), _cand(skills=("Python",), months=12, location="Lyon, France", notice=120))
    assert not result.eligible
    assert [r.rule for r in result.reasons] == ["required_skills", "experience_min", "location", "notice_period"]
