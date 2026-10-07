"""API request/response models."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from engine.schemas import ExtractedJobRequirements, JobRequirements


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth / users -------------------------------------------------------------


class RegisterCompanyIn(BaseModel):
    company_name: str = Field(min_length=2, max_length=200)
    admin_email: EmailStr
    admin_password: str = Field(min_length=12, max_length=256)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_token: str
    company_id: uuid.UUID
    user_id: uuid.UUID
    role: str


class UserIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)
    role: Literal["admin", "recruiter", "hiring_manager"]


class UserOut(_Out):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime


class CompanySettingsIn(BaseModel):
    retention_days: int = Field(ge=1, le=3650)


class CompanyOut(_Out):
    id: uuid.UUID
    name: str
    retention_days: int
    retention_sweep_hour_utc: int | None = None
    candidates_total: int | None = None
    candidates_past_retention: int | None = None


# --- jobs / requirements ------------------------------------------------------


class JobIn(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    jd_text: str = Field(min_length=20, max_length=50_000)


class JobOut(_Out):
    id: uuid.UUID
    title: str
    jd_text: str
    created_at: datetime


class RequirementsIn(ExtractedJobRequirements):
    """Manual requirements edit. Confirmation is a separate step; HR-only knobs are allowed here."""

    required_skills_min_match: int | Literal["all"] | None = None
    score_weights: dict[str, float] | None = None

    def to_engine(self) -> dict[str, Any]:
        return JobRequirements.model_validate(self.model_dump()).model_dump(
            mode="json", exclude={"requirements_confirmed"}
        )


class RequirementsVersionOut(_Out):
    id: uuid.UUID
    version: int
    requirements: dict[str, Any]
    source: str
    analysis_meta: dict[str, Any] | None
    created_at: datetime
    confirmed_by: uuid.UUID | None
    confirmed_at: datetime | None


class ConfirmIn(BaseModel):
    version: int = Field(ge=1)


# --- uploads / resumes --------------------------------------------------------


class SasIn(BaseModel):
    filename: str = Field(min_length=1, max_length=300)
    size: int = Field(gt=0)


class SasOut(BaseModel):
    blob_name: str
    upload_url: str
    expires_at: datetime
    method: Literal["PUT"] = "PUT"
    headers: dict[str, str]


class ConsentIn(BaseModel):
    source: str = Field(min_length=2, max_length=200, description="Where consent was collected")
    purpose: str = Field(min_length=2, max_length=300, description="What the candidate consented to")
    scope: Literal["job", "talent_pool"] = Field(
        default="job", description="talent_pool: the candidate also agreed to be considered for future roles"
    )
    granted_at: datetime | None = None

    @field_validator("granted_at")
    @classmethod
    def valid_consent_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value > datetime.now(UTC)):
            raise ValueError("granted_at must include a timezone and must not be in the future")
        return value


class RegisterIn(BaseModel):
    blob_name: str = Field(min_length=3, max_length=512)
    filename: str = Field(min_length=1, max_length=300)
    job_id: uuid.UUID | None = None
    consent: ConsentIn | None = None


class BulkRegisterIn(BaseModel):
    items: list[RegisterIn] = Field(min_length=1)


class ResumeOut(_Out):
    id: uuid.UUID
    candidate_id: uuid.UUID | None
    version: int | None
    filename: str | None  # None for roles without contact access (filenames often contain names)
    mime: str
    size: int
    status: str
    status_reason: str | None
    processing_generation: int
    processed_generation: int
    created_at: datetime


class RegisterOut(BaseModel):
    resume: ResumeOut
    created: bool


class BulkItemOut(BaseModel):
    blob_name: str
    resume: ResumeOut | None = None
    created: bool = False
    error: str | None = None
    status_code: int = 201


# --- screening ----------------------------------------------------------------


class ScreeningRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["database", "uploads", "both"]


class RunOut(_Out):
    id: uuid.UUID
    job_id: uuid.UUID
    requirements_version: int
    extraction_mode: str
    semantic_backend: str
    versions: dict[str, Any]
    weights: dict[str, Any]
    snapshot: dict[str, Any] | None
    reference_date: date | None
    retry_of_run_id: uuid.UUID | None
    status: str
    total: int
    done: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    blocked: list[Any]
    created_at: datetime
    finished_at: datetime | None


class RunInputOut(_Out):
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    resume_id: uuid.UUID
    resume_version: int
    processing_generation: int
    content_hash: str


class RankedCandidate(BaseModel):
    application_id: uuid.UUID
    candidate_id: uuid.UUID
    rank: int | None
    score: float | None
    eligible: bool | None
    status: str
    reprocess_reason: str | None
    stage: str
    knockout_reasons: list[str]
    explanation: dict[str, Any] | None
    explanation_source: str | None


class CandidatesPage(BaseModel):
    run_id: uuid.UUID
    run_status: str
    total: int
    limit: int
    offset: int
    items: list[RankedCandidate]


class CompareIn(BaseModel):
    job_id: uuid.UUID
    candidate_ids: list[uuid.UUID] = Field(min_length=2, max_length=5)
    run_id: uuid.UUID | None = Field(default=None, description="Run to compare within; default: the job's latest")

    @field_validator("candidate_ids")
    @classmethod
    def _distinct(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(v)) != len(v):
            raise ValueError("candidate_ids must be distinct")
        return v


class DecisionIn(BaseModel):
    decision: Literal["shortlist", "reject", "review"]
    reason: str | None = Field(default=None, max_length=2000)


class TalentSearchIn(BaseModel):
    """Search with free text, a job's description, or both (the text is searched; the job marks who applied)."""

    query: str | None = Field(default=None, min_length=3, max_length=1000)
    job_id: uuid.UUID | None = None
    limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def _something_to_search(self) -> TalentSearchIn:
        if self.query is None and self.job_id is None:
            raise ValueError("Provide a query, a job_id, or both")
        return self


class AddToJobIn(BaseModel):
    candidate_id: uuid.UUID


class AddManyToJobIn(BaseModel):
    candidate_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class UsageOut(BaseModel):
    from_: datetime | None = Field(alias="from")
    to: datetime | None
    totals: dict[str, float]
    by_kind: dict[str, dict[str, float]]
    runs: list[dict[str, Any]]
