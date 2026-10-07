"""Hard filters derived from the (HR-confirmed) job requirements.

Failing candidates are marked ineligible with reasons; they are never dropped,
so recruiters can see and override every knockout. Missing data (no location,
no notice period stated) produces a warning, not a knockout.
"""

from __future__ import annotations

import logging
import math

from engine.config import Settings, get_settings
from engine.logging_setup import log_event
from engine.normalize import SkillOntology, canonical_skill_key, load_ontology, location_matches
from engine.schemas import JobRequirements, KnockoutReason, KnockoutResult, NormalizedCandidate

logger = logging.getLogger("engine.knockout")


def required_skill_threshold(req: JobRequirements, settings: Settings) -> int:
    """How many required skills a candidate must have: the job's N or "all", else ceil(ratio * n)."""
    total = len(req.required_skills)
    if req.required_skills_min_match == "all":
        return total
    if req.required_skills_min_match is not None:
        return req.required_skills_min_match
    return math.ceil(settings.knockout_required_skill_ratio * total)


def missing_required_skills(
    req: JobRequirements, candidate: NormalizedCandidate, ontology: SkillOntology | None = None
) -> list[str]:
    """Required skills (with their accepted alternatives) the candidate has no verified evidence for."""
    ontology = ontology or load_ontology()
    have = {canonical_skill_key(s.canonical, ontology) for s in candidate.skills}
    missing = []
    for skill in req.required_skills:
        if not any(canonical_skill_key(opt, ontology) in have for opt in skill.options):
            missing.append(" or ".join(skill.options))
    return missing


def evaluate_knockouts(
    req: JobRequirements,
    candidate: NormalizedCandidate,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
) -> KnockoutResult:
    settings = settings or get_settings()
    reasons: list[KnockoutReason] = []
    warnings: list[str] = []

    # 1. Required skills: all (default) or N-of-M.
    missing = missing_required_skills(req, candidate, ontology)
    if req.required_skills:
        needed = required_skill_threshold(req, settings)
        matched = len(req.required_skills) - len(missing)
        if matched < needed:
            reasons.append(
                KnockoutReason(
                    rule="required_skills",
                    detail=f"has {matched} of {len(req.required_skills)} required skills (needs {needed}); "
                    f"missing: {', '.join(missing)}",
                )
            )

    # 2. Minimum experience (overlapping jobs counted once).
    if req.experience_min_years:
        required_months = req.experience_min_years * 12
        have_months = candidate.total_experience_months
        if have_months + settings.knockout_experience_tolerance_months < required_months:
            reasons.append(
                KnockoutReason(
                    rule="experience_min",
                    detail=f"{have_months / 12:.1f} years of dated experience; minimum is {req.experience_min_years:g}",
                )
            )
            if candidate.undated_jobs:
                warnings.append(f"{candidate.undated_jobs} job(s) without parseable dates were not counted")

    # 3. Location.
    if req.locations:
        if candidate.location is None:
            warnings.append("location not stated; location rule not applied")
        elif not location_matches(candidate.location, req.locations):
            reasons.append(
                KnockoutReason(
                    rule="location",
                    detail=f"location '{candidate.location}' not in allowed: {', '.join(req.locations)}",
                )
            )

    # 4. Notice period.
    if req.max_notice_period_days is not None:
        if candidate.notice_period_days is None:
            warnings.append("notice period not stated; notice rule not applied")
        elif candidate.notice_period_days > req.max_notice_period_days:
            reasons.append(
                KnockoutReason(
                    rule="notice_period",
                    detail=f"notice period {candidate.notice_period_days} days exceeds maximum "
                    f"{req.max_notice_period_days} days",
                )
            )

    result = KnockoutResult(eligible=not reasons, reasons=reasons, warnings=warnings, missing_required_skills=missing)
    log_event(
        logger,
        "knockouts_evaluated",
        eligible=result.eligible,
        failed_rules=[r.rule for r in reasons],
        warnings=len(warnings),
    )
    return result
