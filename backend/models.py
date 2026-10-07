"""Database schema (SQLAlchemy 2). Every tenant table carries company_id; RLS policies are added in the migration.

Candidate PII lives only in candidate_contacts. Everything a candidate owns cascades from candidates, so a hard
delete is one DELETE plus blob and extraction-cache cleanup (see services/privacy.py).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB, uuid.UUID: UUID(as_uuid=True)}  # noqa: RUF012


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _company_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True, nullable=False)


ROLES = ("admin", "recruiter", "hiring_manager", "employee")
STAGES = ("applied", "screened", "review", "shortlisted", "interview", "offer", "hired", "rejected", "withdrawn")
DECISIONS = ("shortlist", "reject", "review", "move")


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    retention_days: Mapped[int] = mapped_column(Integer, default=180, server_default="180")
    created_at: Mapped[datetime] = _created()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(f"role IN {ROLES}", name="ck_users_role"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-cased; global login identity
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = _created()


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the opaque token
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    title: Mapped[str] = mapped_column(String(300))
    jd_text: Mapped[str] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class JobRequirementsVersion(Base):
    """Immutable once confirmed; edits create a new version."""

    __tablename__ = "job_requirements"
    __table_args__ = (UniqueConstraint("job_id", "version"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    requirements: Mapped[dict[str, Any]]  # engine JobRequirements (without requirements_confirmed)
    source: Mapped[str] = mapped_column(String(20))  # analysis | manual
    analysis_meta: Mapped[dict[str, Any] | None]  # prompt/model/method of the JD analysis
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HiringPlan(Base):
    __tablename__ = "hiring_plans"
    __table_args__ = (UniqueConstraint("job_id", "version"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    rounds: Mapped[list[Any]]
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class HRRecord(Base):
    """Typed, validated HR workflow records; historical decisions are retained in history."""

    __tablename__ = "hr_records"
    __table_args__ = (Index("ix_hr_company_kind", "company_id", "kind"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(30))
    data: Mapped[dict[str, Any]]
    history: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    activity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recruiting_activities.id", ondelete="CASCADE"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hr_records.id", ondelete="CASCADE"), index=True)
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Candidate(Base):
    """No PII here: contact details live in candidate_contacts."""

    __tablename__ = "candidates"
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    created_at: Mapped[datetime] = _created()
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    contact: Mapped[CandidateContact | None] = relationship(back_populates="candidate", passive_deletes=True)


class CandidateContact(Base):
    """PII for HR display and deduplication only. Never read by screening or explanation code."""

    __tablename__ = "candidate_contacts"
    __table_args__ = (
        Index("ix_contacts_email_hash", "company_id", "email_hash"),
        Index("ix_contacts_phone_hash", "company_id", "phone_hash"),
    )
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    email_hash: Mapped[str | None] = mapped_column(String(64))
    phone_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    candidate: Mapped[Candidate] = relationship(back_populates="contact")


class Resume(Base):
    __tablename__ = "resumes"
    __table_args__ = (UniqueConstraint("company_id", "content_hash"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )  # set once the extract_resume job has resolved (or created) the candidate
    version: Mapped[int | None] = mapped_column(Integer)  # 1, 2, ... per candidate
    blob_uri: Mapped[str] = mapped_column(String(1024))
    blob_name: Mapped[str] = mapped_column(String(512))
    filename: Mapped[str] = mapped_column(String(300))
    content_hash: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="registered")  # registered|ready|needs_ocr|failed
    status_reason: Mapped[str | None] = mapped_column(Text)
    # Parsed text with contact details removed: the only resume text the screening path ever sees.
    redacted_text: Mapped[str | None] = mapped_column(Text)
    hidden_items_removed: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    pending_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    pending_consent: Mapped[dict[str, Any] | None]  # consent supplied at registration, attached on resolution
    processing_generation: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    processed_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = _created()


class CandidateAlias(Base):
    __tablename__ = "candidate_aliases"
    __table_args__ = (UniqueConstraint("company_id", "kind", "contact_hash"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    contact_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class ResumeRegistration(Base):
    """Every job association/consent supplied at upload, including duplicates queued before ingestion."""

    __tablename__ = "resume_registrations"
    __table_args__ = (UniqueConstraint("resume_id", "job_id"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    source_type: Mapped[str] = mapped_column(String(30), default="not_recorded", server_default="not_recorded")
    consent: Mapped[dict[str, Any] | None]
    blob_names: Mapped[list[Any]] = mapped_column(default=list, server_default="[]")
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id"),
        CheckConstraint(f"stage IN {STAGES}", name="ck_applications_stage"),
    )
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    source_type: Mapped[str] = mapped_column(String(30), default="not_recorded", server_default="not_recorded")
    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(20), default="applied", server_default="applied")
    created_at: Mapped[datetime] = _created()


class ScreeningRun(Base):
    """Read-only once finished; re-screening creates a new run."""

    __tablename__ = "screening_runs"
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    job_requirements_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_requirements.id", ondelete="CASCADE"))
    requirements_version: Mapped[int] = mapped_column(Integer)
    extraction_mode: Mapped[str] = mapped_column(String(10))
    semantic_backend: Mapped[str] = mapped_column(String(20))
    versions: Mapped[dict[str, Any]]  # engine, prompts, model, ontology
    weights: Mapped[dict[str, Any]]
    snapshot: Mapped[dict[str, Any] | None]
    reference_date: Mapped[date | None] = mapped_column(Date)
    retry_of_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("screening_runs.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|completed|failed|stopped
    total: Mapped[int] = mapped_column(Integer, default=0)
    done: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    blocked: Mapped[list[Any]]  # applications excluded at creation (e.g. no consent)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScreeningInput(Base):
    """Frozen candidate input; removed with candidate erasure, never overwritten by resume reprocessing."""

    __tablename__ = "screening_inputs"
    __table_args__ = (UniqueConstraint("run_id", "application_id"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("screening_runs.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"))
    resume_version: Mapped[int] = mapped_column(Integer)
    processing_generation: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    redacted_text: Mapped[str] = mapped_column(Text)
    extraction: Mapped[dict[str, Any] | None]


class ScreeningResult(Base):
    __tablename__ = "screening_results"
    __table_args__ = (UniqueConstraint("run_id", "application_id"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("screening_runs.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20))  # scored | needs_reprocess
    reprocess_reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    eligible: Mapped[bool | None] = mapped_column(Boolean)
    knockout: Mapped[dict[str, Any] | None]
    features: Mapped[list[Any] | None]
    score_breakdown: Mapped[dict[str, Any] | None]
    explanation: Mapped[dict[str, Any] | None]
    explanation_source: Mapped[str | None] = mapped_column(String(10))
    extraction_meta: Mapped[dict[str, Any] | None]
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _created()


class Decision(Base):
    """Append-only decision history (the app role cannot update or delete rows)."""

    __tablename__ = "decisions"
    __table_args__ = (
        CheckConstraint(f"decision IN {DECISIONS}", name="ck_decisions_decision"),
        Index("ix_decisions_application_created", "application_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    application_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"), index=True)
    decision: Mapped[str] = mapped_column(String(20))
    previous_stage: Mapped[str | None] = mapped_column(String(20))
    new_stage: Mapped[str | None] = mapped_column(String(20))
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()


class RecruitingActivity(Base):
    """Recruiter tasks, interviews, structured feedback, notes and offer records."""

    __tablename__ = "recruiting_activities"
    __table_args__ = (
        CheckConstraint("kind IN ('task', 'interview', 'feedback', 'note', 'offer')", name="ck_recruiting_kind"),
        CheckConstraint(
            "status IN ('open', 'completed', 'cancelled', 'accepted', 'declined')", name="ck_recruiting_status"
        ),
        Index("ix_recruiting_company_due", "company_id", "due_at"),
    )
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    application_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("applications.id", ondelete="CASCADE"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    assigned_to: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(300))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Consent(Base):
    __tablename__ = "consents"
    __table_args__ = (CheckConstraint("scope IN ('job', 'talent_pool')", name="ck_consents_scope"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(200))  # e.g. "careers-site application form v3"
    purpose: Mapped[str] = mapped_column(String(300))  # e.g. "recruitment screening for open roles"
    # "job": screening for the job(s) applied to. "talent_pool": also matching against future roles.
    scope: Mapped[str] = mapped_column(String(20), default="job", server_default="job")
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()


class AuditLog(Base):
    """Append-only (the app role has no UPDATE/DELETE). Never store contact details here."""

    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # no FK: survives user deletion
    actor: Mapped[str] = mapped_column(String(100))  # "user" | "system:retention" | ...
    action: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str] = mapped_column(String(50))
    object_id: Mapped[str | None] = mapped_column(String(100))
    before: Mapped[dict[str, Any] | None]
    after: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = _created()


class ExtractionCacheEntry(Base):
    """Job-independent extraction results, per company (engine cache key + company_id)."""

    __tablename__ = "extraction_cache"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)  # resume file hash, for deletion
    result: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = _created()


class QueueJob(Base):
    __tablename__ = "queue_jobs"
    __table_args__ = (Index("ix_queue_claim", "status", "priority", "run_after"),)
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    kind: Mapped[str] = mapped_column(String(50))
    # Lower runs first: interactive work (0) is never queued behind maintenance (1) or bulk background work (2).
    priority: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    idempotency_key: Mapped[str] = mapped_column(String(300), unique=True)
    payload: Mapped[dict[str, Any]]  # ids only, never PII
    status: Mapped[str] = mapped_column(String(10), default="queued")  # queued|running|done|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ErasureTombstone(Base):
    """Keyed contact hash (HMAC) of an erased candidate, kept briefly so an upload registered before the erasure
    cannot recreate them when its ingestion job runs later. Holds no contact details."""

    __tablename__ = "erasure_tombstones"
    __table_args__ = (
        CheckConstraint("kind IN ('email', 'phone')", name="ck_erasure_tombstones_kind"),
        Index("ix_erasure_tombstones_lookup", "company_id", "contact_hash"),
    )
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(10))
    contact_hash: Mapped[str] = mapped_column(String(64))
    erased_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CandidateEmbedding(Base):
    """A resume's vector for talent-pool search, from its redacted text (no contact details).

    Deleted with its resume/candidate (erasure and retention). Search compares only vectors of one model.
    """

    __tablename__ = "candidate_embeddings"
    __table_args__ = (
        UniqueConstraint("resume_id", "model"),
        Index("ix_candidate_embeddings_search", "company_id", "model"),
    )
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"))
    model: Mapped[str] = mapped_column(String(200))
    dimension: Mapped[int] = mapped_column(Integer)
    vector: Mapped[list[float]] = mapped_column(ARRAY(REAL))
    created_at: Mapped[datetime] = _created()


class PoolMatch(Base):
    """Eligibility of a talent-pool candidate for one confirmed requirements version (a suggestion for HR).

    Computed with the same extraction and knockout rules as screening; it never creates an application.
    """

    __tablename__ = "pool_matches"
    __table_args__ = (
        UniqueConstraint("requirements_id", "candidate_id"),
        CheckConstraint("status IN ('pending', 'checked', 'failed')", name="ck_pool_matches_status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    requirements_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_requirements.id", ondelete="CASCADE"))
    requirements_version: Mapped[int] = mapped_column(Integer)
    candidate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("candidates.id", ondelete="CASCADE"), index=True)
    resume_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    eligible: Mapped[bool | None] = mapped_column(Boolean)
    knockout: Mapped[dict[str, Any] | None]  # reasons, warnings, missing_required_skills
    failure_reason: Mapped[str | None] = mapped_column(Text)
    similarity: Mapped[float | None] = mapped_column(Float)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class UsageEvent(Base):
    __tablename__ = "usage_events"
    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = _company_fk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("screening_runs.id", ondelete="SET NULL"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # llm | ocr
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _created()


# Tables with a company_id column (row-level security applies to all of these).
TENANT_TABLES = [t.name for t in Base.metadata.sorted_tables if "company_id" in t.columns and t.name != "companies"]
