"""Deterministic features: each returns {name, value in [0, 1], detail, evidence[]}.

Features only read verified extraction output (via ``NormalizedCandidate``):
skills, jobs and projects whose evidence quotes were found in the resume. The
free-text summary is not part of NormalizedCandidate, so it can never
influence a score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from engine.config import Settings, get_settings
from engine.normalize import (
    SkillOntology,
    YearMonth,
    canonical_skill_key,
    load_ontology,
    merge_intervals,
    months_inclusive,
    title_to_seniority,
    ym_from_str,
)
from engine.schemas import (
    CandidateSkill,
    FeatureResult,
    JobRequirements,
    NormalizedCandidate,
    RequirementSkill,
    SeniorityLevel,
)
from engine.semantic import SemanticScorer

FEATURE_NAMES: tuple[str, ...] = (
    "required_skill_coverage",
    "relevant_experience",
    "project_relevance",
    "seniority_fit",
    "preferred_skill_coverage",
    "semantic_relevance",
)
_LEVELS = {"intern": 1, "junior": 2, "mid": 3, "senior": 4, "lead": 5, "principal": 6}
_TITLE_STOPWORDS = {
    "senior", "sr", "junior", "jr", "lead", "staff", "principal", "head", "intern", "associate", "chief",
    "of", "and", "the", "for", "in", "i", "ii", "iii", "iv", "part", "time", "full", "contract", "freelance",
}  # fmt: skip
_TITLE_EXPANSIONS = {"ml": ["machine", "learning"], "ai": ["artificial", "intelligence"], "ds": ["data", "science"]}
# A job mentioning this many of the JD's skills is fully skill-relevant.
_SKILL_OVERLAP_SATURATION = 3


@dataclass(frozen=True)
class FeatureContext:
    requirements: JobRequirements
    candidate: NormalizedCandidate
    semantic: SemanticScorer
    job_text: str
    settings: Settings
    ontology: SkillOntology
    reranker: SemanticScorer | None = None


def build_context(
    requirements: JobRequirements,
    candidate: NormalizedCandidate,
    semantic: SemanticScorer,
    settings: Settings | None = None,
    ontology: SkillOntology | None = None,
    reranker: SemanticScorer | None = None,
) -> FeatureContext:
    return FeatureContext(
        requirements=requirements,
        candidate=candidate,
        semantic=semantic,
        job_text=job_profile_text(requirements),
        settings=settings or get_settings(),
        ontology=ontology or load_ontology(),
        reranker=reranker,
    )


def compute_features(ctx: FeatureContext) -> list[FeatureResult]:
    return [
        required_skill_coverage(ctx),
        relevant_experience(ctx),
        project_relevance(ctx),
        seniority_fit(ctx),
        preferred_skill_coverage(ctx),
        semantic_relevance(ctx),
    ]


# ---------------------------------------------------------------------------
# Text profiles (verified data only)
# ---------------------------------------------------------------------------


def job_profile_text(req: JobRequirements) -> str:
    """The job as text: role, the JD quotes behind each skill, and the skill names."""
    lines = [req.role]
    for skill in [*req.required_skills, *req.preferred_skills]:
        lines.append(skill.evidence or " or ".join(skill.options))
        lines.append(" ".join(skill.options))
    return "\n".join(dict.fromkeys(line for line in lines if line))


def candidate_profile_text(cand: NormalizedCandidate) -> str:
    """Verified skills, job titles/descriptions and projects. Never the summary."""
    parts = [", ".join(s.canonical for s in cand.skills)]
    for job in cand.jobs:
        parts.append(job.title)
        if job.description:
            parts.append(job.description)
    parts.extend(_project_text(p.name, p.description, p.technologies) for p in cand.projects)
    return "\n".join(p for p in parts if p)


def _project_text(name: str, description: str | None, technologies: list[str]) -> str:
    return "\n".join(filter(None, [name, description, ", ".join(technologies)]))


# ---------------------------------------------------------------------------
# Skill coverage
# ---------------------------------------------------------------------------


def skill_recency_weight(skill: CandidateSkill, reference: YearMonth, settings: Settings) -> float:
    """Used in a dated role within 3 years: 1.0; older use decays linearly to the floor (0.6) at 10 years;
    listed only (no dated role): 0.5 — always below any dated use."""
    if skill.last_used is None:
        return settings.skill_unknown_recency_weight
    age = reference.index - ym_from_str(skill.last_used).index
    full, floor_at = settings.skill_recency_full_months, settings.skill_recency_floor_months
    if age <= full:
        return 1.0
    if age >= floor_at:
        return settings.skill_recency_min_weight
    return 1.0 - (age - full) / (floor_at - full) * (1.0 - settings.skill_recency_min_weight)


def not_applicable(name: str, reason: str) -> FeatureResult:
    return FeatureResult(name=name, applicable=False, value=0.0, detail=f"not applicable: {reason}")


def _coverage(name: str, wanted: list[RequirementSkill], ctx: FeatureContext) -> FeatureResult:
    if not wanted:
        kind = "required" if name.startswith("required") else "preferred"
        return not_applicable(name, f"the job lists no {kind} skills")
    reference = ym_from_str(ctx.candidate.reference_month)
    by_key: dict[str, CandidateSkill] = {}
    for s in ctx.candidate.skills:
        by_key.setdefault(canonical_skill_key(s.canonical, ctx.ontology), s)

    total = 0.0
    evidence: list[str] = []
    found: list[str] = []
    missing: list[str] = []
    for req in wanted:
        matches = [by_key[k] for opt in req.options if (k := canonical_skill_key(opt, ctx.ontology)) in by_key]
        if not matches:
            missing.append(" or ".join(req.options))
            continue
        best = max(matches, key=lambda s: skill_recency_weight(s, reference, ctx.settings))
        weight = skill_recency_weight(best, reference, ctx.settings)
        total += weight
        found.append(f"{best.canonical} ({_recency_label(best)}, w={weight:.2f})")
        evidence.append(best.evidence)

    value = total / len(wanted)
    detail = f"{len(found)}/{len(wanted)} evidenced, recency-weighted {value:.2f}: {'; '.join(found) or 'none'}"
    if missing:
        detail += f". Missing: {', '.join(missing)}"
    return FeatureResult(name=name, value=round(value, 4), detail=detail, evidence=list(dict.fromkeys(evidence)))


def _recency_label(skill: CandidateSkill) -> str:
    return f"last used {skill.last_used}" if skill.last_used else "not tied to a dated role"


def required_skill_coverage(ctx: FeatureContext) -> FeatureResult:
    return _coverage("required_skill_coverage", ctx.requirements.required_skills, ctx)


def preferred_skill_coverage(ctx: FeatureContext) -> FeatureResult:
    return _coverage("preferred_skill_coverage", ctx.requirements.preferred_skills, ctx)


# ---------------------------------------------------------------------------
# Experience & seniority
# ---------------------------------------------------------------------------


def _title_tokens(title: str) -> set[str]:
    tokens: list[str] = []
    for word in re.findall(r"[a-z0-9+#]+", title.lower()):
        tokens.extend(_TITLE_EXPANSIONS.get(word, [word]))
    return {t for t in tokens if t not in _TITLE_STOPWORDS}


_DOMAIN_SPLIT = re.compile(r"\s*(?:,|/|;|\bor\b|\band\b)\s*", re.IGNORECASE)
_DOMAIN_STOPWORDS = {
    "roles",
    "role",
    "positions",
    "position",
    "field",
    "fields",
    "related",
    "similar",
    "a",
    "an",
    "the",
}


def _stems(text: str) -> set[str]:
    """Crude 5-character stems, so "data scientist" matches the domain "data science"."""
    return {t[:5] for t in _title_tokens(text)}


def domain_match(domain: str | None, job_text: str) -> bool:
    """True if every word of any one phrase of the JD's experience domain appears in the job's text."""
    if not domain:
        return False
    job_stems = _stems(job_text)
    for phrase in _DOMAIN_SPLIT.split(domain):
        words = [w for w in re.findall(r"[a-z0-9+#]+", phrase.lower()) if w not in _DOMAIN_STOPWORDS]
        stems = _stems(" ".join(words))
        if stems and stems <= job_stems:
            return True
    return False


def job_relevance(title: str, job_text: str, ctx: FeatureContext) -> float:
    """How relevant a role is to the job's experience requirement.

    When the JD states an experience domain ("1+ years in data analysis"), only roles in that domain count:
    max(title similarity to the JD role, domain match). Sharing generic skills (Python, SQL) does not make a
    role "experience in data analysis". Without a stated domain, overlap with the JD's skills also counts.
    """
    role_tokens = _title_tokens(ctx.requirements.role)
    title_sim = len(_title_tokens(title) & role_tokens) / len(role_tokens) if role_tokens else 0.0
    domain = ctx.requirements.experience_domain
    if domain:
        return max(title_sim, 1.0 if domain_match(domain, job_text) else 0.0)
    jd_skills = {
        canonical_skill_key(opt, ctx.ontology)
        for s in [*ctx.requirements.required_skills, *ctx.requirements.preferred_skills]
        for opt in s.options
    }
    mentioned = {canonical_skill_key(m.canonical, ctx.ontology) for m in ctx.ontology.find_mentions(job_text)}
    saturation = min(_SKILL_OVERLAP_SATURATION, len(jd_skills)) or 1
    skill_sim = min(1.0, len(mentioned & jd_skills) / saturation)
    return max(title_sim, skill_sim)


def experience_score(relevant_months: int, min_years: float | None) -> float:
    """r = relevant / required. 0.8*r below the minimum; 0.8 -> 1.0 linearly from 1x to 1.5x; capped at 1.0.

    With no stated minimum: 1.0 if there is any relevant experience, else 0.5.
    """
    if not min_years:
        return 1.0 if relevant_months > 0 else 0.5
    r = relevant_months / (min_years * 12)
    if r < 1:
        return 0.8 * r
    return min(1.0, 0.8 + 0.2 * (r - 1) / 0.5)


def relevant_experience(ctx: FeatureContext) -> FeatureResult:
    """Months in roles relevant to the JD title, experience domain or skills (overlaps counted once)."""
    intervals = []
    counted: list[str] = []
    evidence: list[str] = []
    for job in ctx.candidate.jobs:
        if not (job.start and job.end):
            continue
        text = "\n".join(filter(None, [job.title, job.description, job.evidence]))
        rel = job_relevance(job.title, text, ctx)
        if rel >= ctx.settings.relevant_job_threshold:
            intervals.append((ym_from_str(job.start), ym_from_str(job.end)))
            counted.append(f"{job.title} ({job.months} mo, relevance {rel:.2f})")
            evidence.append(job.evidence)
    months = sum(months_inclusive(iv) for iv in merge_intervals(intervals))
    min_years = ctx.requirements.experience_min_years
    value = experience_score(months, min_years)
    domain = f" in {ctx.requirements.experience_domain}" if ctx.requirements.experience_domain else ""
    if min_years:
        basis = (
            f"{months} relevant months vs {min_years * 12:g} required{domain} (ratio {months / (min_years * 12):.2f})"
        )
    else:
        basis = f"{months} relevant months; the job states no minimum"
    detail = f"{basis}. Counted: {'; '.join(counted) or 'none'}"
    return FeatureResult(name="relevant_experience", value=round(value, 4), detail=detail, evidence=evidence)


def jd_seniority_level(req: JobRequirements) -> int:
    return _LEVELS[req.seniority] if req.seniority else int(title_to_seniority(req.role))


def seniority_fit(ctx: FeatureContext) -> FeatureResult:
    jd_level = jd_seniority_level(ctx.requirements)
    cand_level = ctx.candidate.seniority
    value = max(0.0, 1.0 - 0.25 * abs(cand_level - jd_level))
    latest = ctx.candidate.jobs[0] if ctx.candidate.jobs else None
    basis = "" if latest else " (no roles listed: entry level)"
    detail = (
        f"candidate level {cand_level} ({SeniorityLevel(cand_level).name.lower()}){basis} vs job level {jd_level} "
        f"({SeniorityLevel(jd_level).name.lower()})"
    )
    return FeatureResult(name="seniority_fit", value=value, detail=detail, evidence=[latest.evidence] if latest else [])


# ---------------------------------------------------------------------------
# Semantic features
# ---------------------------------------------------------------------------


def project_relevance(ctx: FeatureContext) -> FeatureResult:
    """Mean of the top-3 similarities between projects and the job.

    Without a projects section, the verified bullet lines of the candidate's job descriptions are used
    instead; the feature is N/A only when neither exists.
    """
    items: list[tuple[str, str, str]]  # (label, text, evidence quote)
    if ctx.candidate.projects:
        source = "project"
        items = [
            (p.name, _project_text(p.name, p.description, p.technologies), p.evidence) for p in ctx.candidate.projects
        ]
    else:
        source = "job bullet"
        items = [
            (f"{job.title}: {line[:60]}", line, line)
            for job in ctx.candidate.jobs
            for line in (job.description or "").split("\n")
            if line.strip()
        ]
    if not items:
        return not_applicable("project_relevance", "no projects or job-description bullets")
    sims = ctx.semantic.similarities(ctx.job_text, [text for _, text, _ in items])
    ranked = sorted(zip(sims, items, strict=True), key=lambda si: si[0], reverse=True)[:3]
    value = sum(sim for sim, _ in ranked) / len(ranked)
    detail = f"mean of top-{len(ranked)} {source} similarities ({ctx.semantic.name}): " + "; ".join(
        f"{label}={sim:.2f}" for sim, (label, _, _) in ranked
    )
    return FeatureResult(
        name="project_relevance",
        value=round(value, 4),
        detail=detail,
        evidence=list(dict.fromkeys(ev for _, (_, _, ev) in ranked)),
    )


def semantic_relevance(ctx: FeatureContext) -> FeatureResult:
    profile = candidate_profile_text(ctx.candidate)
    if not profile.strip():
        return FeatureResult(name="semantic_relevance", value=0.0, detail="no verified content to compare")
    scorer = ctx.reranker or ctx.semantic
    value = scorer.similarities(ctx.job_text, [profile])[0]
    return FeatureResult(
        name="semantic_relevance",
        value=round(value, 4),
        detail=f"{scorer.name} similarity between the job profile and verified skills/jobs/projects",
    )
