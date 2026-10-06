"""Data contracts.

Extraction models are deliberately free of personal identifiers: there are NO
fields for name, email, phone, photo, gender, age or street address. Every
extracted item carries ``evidence`` — a verbatim quote from the source text —
which is verified after extraction (see ``extraction.verify_evidence``).

The LLM-facing models (CandidateExtraction, JobRequirements) avoid field
defaults so they compile to OpenAI strict JSON schemas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


class SeniorityLevel(IntEnum):
    INTERN = 1
    JUNIOR = 2
    MID = 3
    SENIOR = 4
    LEAD = 5
    PRINCIPAL = 6


SeniorityName = Literal["intern", "junior", "mid", "senior", "lead", "principal"]


def _require_non_negative(v: float) -> float:
    # Enforced in Python rather than via JSON-schema "minimum", which Azure
    # structured outputs does not reliably support.
    if v < 0:
        raise ValueError("must be >= 0")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class ParsedDocument(BaseModel):
    source_name: str
    file_type: Literal["pdf", "docx", "txt"]
    text: str
    page_count: int = Field(ge=0)
    char_count: int = Field(ge=0)
    hidden_items_removed: int = Field(ge=0, description="PDF spans / DOCX runs stripped as hidden")
    hidden_chars_removed: int = Field(ge=0)
    hidden_reasons: dict[str, int] = Field(default_factory=dict)
    needs_ocr: bool = False
    ocr_applied: bool = False
    ocr_pages: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Candidate extraction (LLM response format + rule-based output)
# ---------------------------------------------------------------------------


class SkillItem(_Strict):
    name: str = Field(description="Skill name as written in the resume")
    evidence: str = Field(description="Exact quote from the resume that mentions this skill")


class JobItem(_Strict):
    title: str = Field(description="Job title copied verbatim")
    company: str | None = Field(description="Employer name, or null")
    start: str | None = Field(description="Start date exactly as written, e.g. 'Jan 2021', '2021-01', or null")
    end: str | None = Field(description="End date exactly as written, 'Present' if current, or null")
    description: str | None = Field(description="Responsibilities/achievements text for this role, or null")
    evidence: str = Field(description="Exact quote containing the title and dates of this role")


class ProjectItem(_Strict):
    name: str
    description: str | None
    technologies: list[str]
    evidence: str = Field(description="Exact quote describing the project")


class EducationItem(_Strict):
    degree: str
    field: str | None
    institution: str | None
    year: str | None
    evidence: str


class CertificationItem(_Strict):
    name: str
    issuer: str | None
    year: str | None
    evidence: str


class LocationItem(_Strict):
    value: str = Field(description="City/region/country or 'Remote' — never a street address")
    evidence: str


class NoticePeriodItem(_Strict):
    days: int = Field(description="Notice period converted to days (immediate = 0, 1 month = 30)")
    evidence: str

    @field_validator("days")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        return _require_non_negative(v)


class CandidateExtraction(_Strict):
    skills: list[SkillItem]
    jobs: list[JobItem]
    projects: list[ProjectItem]
    education: list[EducationItem]
    certifications: list[CertificationItem]
    location: LocationItem | None
    notice_period_days: NoticePeriodItem | None
    summary: str | None = Field(
        description="1-2 sentence neutral professional summary; no names or personal data. Not used for scoring."
    )


# ---------------------------------------------------------------------------
# Job requirements
# ---------------------------------------------------------------------------


class RequirementSkill(_Strict):
    name: str
    # Required from the LLM; may be null when HR edits requirements by hand.
    evidence: str | None = Field(description="Exact quote from the job description")
    # "Azure or AWS" => name="Azure", alternatives=["AWS"]: any one of them satisfies the requirement.
    alternatives: list[str] = Field(
        default_factory=list, description="Other skills the posting accepts instead of this one ('X or Y'), else []"
    )

    @property
    def options(self) -> list[str]:
        return [self.name, *self.alternatives]


class ExtractedJobRequirements(_Strict):
    """LLM response format for a JD. HR-only fields live on ``JobRequirements``."""

    role: str
    seniority: SeniorityName | None
    experience_min_years: float | None
    # "5+ years in machine learning or data science roles" => "machine learning or data science".
    experience_domain: str | None = Field(
        default=None, description="Field/domain the required years must be in, as stated; null if not stated"
    )
    required_skills: list[RequirementSkill]
    preferred_skills: list[RequirementSkill]
    locations: list[str] = Field(description="Acceptable locations; include 'Remote' if remote is allowed")
    max_notice_period_days: int | None

    @field_validator("experience_min_years", "max_notice_period_days")
    @classmethod
    def _non_negative(cls, v: float | None) -> float | None:
        return None if v is None else _require_non_negative(v)


class RequirementsConfirmation(_Strict):
    by: str = Field(min_length=1, description="Who reviewed and approved the requirements")
    at: datetime


class JobRequirements(ExtractedJobRequirements):
    """Requirements as reviewed by HR. Screening refuses to run until ``requirements_confirmed`` is set."""

    requirements_confirmed: RequirementsConfirmation | None = None
    # Knockout: None => every required skill must be present; N => at least N of them.
    # Knockout threshold: an int N (at least N required skills), "all", or None for the configured
    # default ratio (ceil(0.8 * number of required skills)).
    required_skills_min_match: int | Literal["all"] | None = Field(default=None)
    # Per-job override of the scoring weights (feature name -> weight); None => config defaults.
    score_weights: dict[str, float] | None = None

    @model_validator(mode="after")
    def _min_match_within_bounds(self) -> JobRequirements:
        n = self.required_skills_min_match
        if isinstance(n, int) and n < 1:
            raise ValueError("required_skills_min_match must be >= 1")
        if isinstance(n, int) and n > len(self.required_skills):
            raise ValueError(
                f"required_skills_min_match={n} exceeds the number of required skills ({len(self.required_skills)})"
            )
        return self

    def confirm(self, by: str, at: datetime | None = None) -> JobRequirements:
        return self.model_copy(
            update={"requirements_confirmed": RequirementsConfirmation(by=by, at=at or datetime.now(UTC))}
        )


# ---------------------------------------------------------------------------
# Extraction run metadata
# ---------------------------------------------------------------------------


DropReason = Literal["evidence_not_found", "evidence_mismatch", "evidence_too_short", "dates_not_verified"]


class DroppedItem(BaseModel):
    field: str
    reason: DropReason
    evidence: str


class FieldEvidenceStats(BaseModel):
    total: int = Field(ge=0)
    dropped: int = Field(ge=0)

    @property
    def drop_rate(self) -> float:
        return self.dropped / self.total if self.total else 0.0


def evidence_stats_summary(stats: dict[str, FieldEvidenceStats]) -> dict[str, float]:
    """Per-field drop rate plus an ``overall`` rate, rounded for logs and API output."""
    total = sum(s.total for s in stats.values())
    dropped = sum(s.dropped for s in stats.values())
    out = {name: round(s.drop_rate, 3) for name, s in stats.items() if s.total}
    out["overall"] = round(dropped / total, 3) if total else 0.0
    return out


class LLMUsage(BaseModel):
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0


class ExtractionResult(BaseModel):
    status: Literal["ok", "extraction_failed"]
    extraction: CandidateExtraction | None  # None iff status == "extraction_failed"
    failure_reason: str | None = None
    method: Literal["llm", "rules"]
    prompt_name: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    ontology_version: str | None = None
    dropped_items: list[DroppedItem] = Field(default_factory=list)
    evidence_stats: dict[str, FieldEvidenceStats] = Field(default_factory=dict)
    # Indices into extraction.jobs whose dates were matched on the minority side of the resume's layout.
    low_confidence_jobs: list[int] = Field(default_factory=list)
    input_truncated: bool = False
    attempts: int = 0
    from_cache: bool = False
    usage: LLMUsage | None = None

    @model_validator(mode="after")
    def _status_consistent(self) -> ExtractionResult:
        if (self.status == "ok") != (self.extraction is not None):
            raise ValueError("extraction must be set iff status == 'ok'")
        if self.status == "extraction_failed" and not self.failure_reason:
            raise ValueError("failure_reason is required when extraction failed")
        return self

    @property
    def dropped_count(self) -> int:
        return len(self.dropped_items)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence_drop_rates(self) -> dict[str, float]:
        return evidence_stats_summary(self.evidence_stats)


class JobAnalysisResult(BaseModel):
    requirements: JobRequirements
    method: Literal["llm", "rules"]
    prompt_name: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    ontology_version: str | None = None
    dropped_items: list[DroppedItem] = Field(default_factory=list)
    evidence_stats: dict[str, FieldEvidenceStats] = Field(default_factory=dict)
    attempts: int = 0
    usage: LLMUsage | None = None


# ---------------------------------------------------------------------------
# Normalized candidate (Phase 2)
# ---------------------------------------------------------------------------


class NormalizedJob(BaseModel):
    title: str
    company: str | None
    start: str | None = Field(description="YYYY-MM, or None if the dates could not be parsed")
    end: str | None = Field(description="YYYY-MM (the reference month for current roles)")
    is_current: bool
    months: int = Field(ge=0, description="Inclusive month count; 0 when undated")
    date_confidence: Literal["high", "low"] | None = Field(
        description="high: full month range; low: year-only or start-only dates (inferred); None: undated"
    )
    date_note: str | None = Field(default=None, description="How incomplete dates were interpreted")
    seniority: int = Field(ge=1, le=6)
    description: str | None
    evidence: str


class CandidateSkill(BaseModel):
    canonical: str
    raw: str
    evidence: str
    in_ontology: bool
    last_used: str | None = Field(description="YYYY-MM end of the latest job mentioning the skill, if any")


class NormalizedCandidate(BaseModel):
    skills: list[CandidateSkill]
    jobs: list[NormalizedJob]  # most recent first
    projects: list[ProjectItem]
    total_experience_months: int = Field(ge=0, description="Union of job intervals: overlaps counted once")
    undated_jobs: int = Field(ge=0)
    seniority: int = Field(ge=1, le=6, description="Level of the most recent job; 1 (entry) with no jobs")
    location: str | None
    notice_period_days: int | None
    reference_month: str = Field(description="YYYY-MM used for 'present' and recency")

    @property
    def skill_names(self) -> set[str]:
        return {s.canonical for s in self.skills}


# ---------------------------------------------------------------------------
# Knockouts (Phase 2)
# ---------------------------------------------------------------------------

KnockoutRule = Literal["required_skills", "experience_min", "location", "notice_period"]


class KnockoutReason(BaseModel):
    rule: KnockoutRule
    detail: str


class KnockoutResult(BaseModel):
    eligible: bool
    reasons: list[KnockoutReason] = Field(default_factory=list)
    # Rules that could not be evaluated (missing data): flagged for HR, never an automatic knockout.
    warnings: list[str] = Field(default_factory=list)
    missing_required_skills: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Features & scoring (Phase 3)
# ---------------------------------------------------------------------------


class FeatureResult(BaseModel):
    name: str
    applicable: bool = Field(default=True, description="False when the feature cannot be assessed (N/A)")
    value: float = Field(ge=0, le=1, description="0 when not applicable; ignored by the scorer")
    detail: str
    evidence: list[str] = Field(default_factory=list, description="Verified quotes supporting the value")


class ScoreBreakdown(BaseModel):
    score: float = Field(ge=0, le=100)
    configured_weights: dict[str, float]  # normalized over all features
    weights: dict[str, float]  # effective: renormalized over applicable features (N/A => 0)
    not_applicable: list[str]
    contributions: dict[str, float]  # feature -> points (effective weight * value * 100)


# ---------------------------------------------------------------------------
# Explanations (Phase 4)
# ---------------------------------------------------------------------------


class StrengthDraft(_Strict):
    feature: str = Field(description="Name of the feature this strength is based on")
    text: str = Field(description="One sentence")
    quote: str = Field(description="Exact quote copied from that feature's evidence list")


class GapDraft(_Strict):
    feature: str = Field(description="Name of the feature this gap is based on")
    text: str = Field(description="One sentence")


class ExplanationDraft(_Strict):
    """LLM response format. Limits (4 + 4) and quotes are enforced after the call."""

    strengths: list[StrengthDraft]
    gaps: list[GapDraft]


class Strength(BaseModel):
    feature: str
    text: str
    quote: str = Field(description="Verified evidence quote from the resume")


class Gap(BaseModel):
    feature: str | None
    text: str
    source: Literal["missing_required_skills", "knockout", "feature"]


class DroppedStrength(BaseModel):
    text: str
    quote: str
    reason: Literal["quote_not_in_evidence", "unknown_or_na_feature", "over_limit", "duplicate", "mentions_career_gap"]


class Explanation(BaseModel):
    strengths: list[Strength] = Field(max_length=4)
    gaps: list[Gap] = Field(max_length=4)
    # Data-quality caveats shown alongside the explanation (low-confidence dates, unapplied rules).
    warnings: list[str] = Field(default_factory=list)
    method: Literal["llm", "template"]
    prompt_name: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    dropped_strengths: list[DroppedStrength] = Field(default_factory=list)
    dropped_gaps: list[str] = Field(default_factory=list, description="LLM gaps removed by post-checks")
    fallback_reason: str | None = Field(default=None, description="Set when the LLM failed and templates were used")
    usage: LLMUsage | None = None


# ---------------------------------------------------------------------------
# Screening run output
# ---------------------------------------------------------------------------


class CandidateExtractionMeta(BaseModel):
    method: Literal["llm", "rules"]
    prompt_name: str | None
    prompt_version: str | None
    model: str | None
    ontology_version: str | None
    attempts: int
    dropped_items: int
    evidence_drop_rates: dict[str, float]
    input_truncated: bool
    from_cache: bool = False
    usage: LLMUsage | None


class CandidateResult(BaseModel):
    candidate_id: str = Field(description="Content hash of the uploaded file")
    source_name: str
    rank: int | None = Field(ge=1, description="Position among eligible candidates; None if ineligible")
    score: float = Field(ge=0, le=100)
    eligible: bool
    knockout: KnockoutResult
    features: list[FeatureResult]
    score_breakdown: ScoreBreakdown
    total_experience_months: int
    hidden_items_removed: int
    extraction: CandidateExtractionMeta
    explanation: Explanation
    explanation_source: Literal["llm", "template"] = Field(description="Who wrote the explanation (for UI labels)")


class ReprocessItem(BaseModel):
    candidate_id: str | None
    source_name: str
    status: Literal["extraction_failed", "needs_ocr", "parse_error", "duplicate"]
    reason: str


class RunMetadata(BaseModel):
    engine_version: str
    started_at: datetime
    reference_month: str
    extraction_mode: Literal["llm", "rules"]
    prompt_name: str | None
    prompt_version: str | None
    model: str | None
    semantic_backend: str
    reranker: str | None
    ontology_version: str
    weights: dict[str, float]
    explanation_method: Literal["llm", "template"]
    explanation_prompt_version: str | None
    requirements_confirmed: RequirementsConfirmation
    documents_received: int
    llm_prompt_tokens: int = 0
    llm_completion_tokens: int = 0
    llm_cost_usd: float = 0.0
    embedding_tokens: int = 0
    embedding_cost_usd: float = 0.0
    extraction_cache_hits: int = 0
    extraction_cache_misses: int = 0
    max_workers: int = 1
    wall_time_ms: float = 0.0


class ScreeningResult(BaseModel):
    job_role: str
    run: RunMetadata
    ranked: list[CandidateResult] = Field(description="Eligible candidates, ranked by score")
    ineligible: list[CandidateResult] = Field(description="Knocked-out candidates (with reasons), by score")
    needs_reprocess: list[ReprocessItem]
