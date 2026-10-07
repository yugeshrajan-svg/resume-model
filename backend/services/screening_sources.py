"""Tenant-scoped resume choices for a screening, without changing applications on preview."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, select

from backend.db import TenantRepo
from backend.models import Application, Consent, Job, Resume, ResumeRegistration

ResumeSource = Literal["database", "uploads", "both"]


def consenting_candidates(company_id: uuid.UUID, *, pool: bool = False):
    query = select(Consent.candidate_id).where(
        Consent.company_id == company_id,
        Consent.revoked_at.is_(None),
        Consent.granted_at <= func.now(),
    )
    return query.where(Consent.scope == "talent_pool") if pool else query


@dataclass
class ResumeSources:
    database: dict[uuid.UUID, uuid.UUID]
    uploads: dict[uuid.UUID, uuid.UUID]
    pending_uploads: int
    database_excluded: int

    def selected(self, source: ResumeSource) -> dict[uuid.UUID, uuid.UUID]:
        if source == "database":
            return self.database.copy()
        if source == "uploads":
            return self.uploads.copy()
        # One candidate per run. A job upload takes precedence when a candidate appears in both sources.
        return {**self.database, **self.uploads}

    def requires_pool_consent(self, source: ResumeSource, candidate_id: uuid.UUID) -> bool:
        return source == "database" or (source == "both" and candidate_id not in self.uploads)


async def load_sources(repo: TenantRepo, job_id: uuid.UUID) -> ResumeSources:
    await repo.get(Job, job_id)
    registered = select(ResumeRegistration.resume_id).where(
        ResumeRegistration.company_id == repo.company_id, ResumeRegistration.job_id == job_id
    )
    # Registration records distinguish uploads from candidates attached by a previous database screening.
    # pending_job_id also covers uploads made before durable registration records were introduced.
    uploaded_resumes = select(Resume.id).where(
        Resume.company_id == repo.company_id,
        (Resume.id.in_(registered)) | (Resume.pending_job_id == job_id),
    )
    ranked_resumes = select(
        Resume.candidate_id,
        Resume.id,
        func.row_number()
        .over(
            partition_by=Resume.candidate_id,
            order_by=[Resume.version.desc().nulls_last(), Resume.created_at.desc(), Resume.id],
        )
        .label("position"),
    ).where(Resume.company_id == repo.company_id)
    job_candidates = select(Application.candidate_id).where(
        Application.company_id == repo.company_id, Application.job_id == job_id
    )
    # An application can point to a newer saved version after database screening. Keep its original
    # job uploads available by choosing the latest uploaded version independently of that pointer.
    latest_uploaded = ranked_resumes.where(
        Resume.id.in_(uploaded_resumes), Resume.candidate_id.in_(job_candidates)
    ).subquery()
    uploaded = (
        await repo.session.execute(
            select(latest_uploaded.c.candidate_id, latest_uploaded.c.id).where(latest_uploaded.c.position == 1)
        )
    ).all()
    latest = ranked_resumes.where(
        Resume.status == "ready",
        Resume.candidate_id.in_(consenting_candidates(repo.company_id, pool=True)),
    ).subquery()
    saved = (await repo.session.execute(select(latest.c.candidate_id, latest.c.id).where(latest.c.position == 1))).all()
    pending = await repo.session.scalar(
        select(func.count())
        .select_from(Resume)
        .where(Resume.company_id == repo.company_id, Resume.id.in_(uploaded_resumes), Resume.status == "registered")
    )
    total_saved = await repo.session.scalar(
        select(func.count(func.distinct(Resume.candidate_id))).where(Resume.company_id == repo.company_id)
    )
    return ResumeSources(dict(saved), dict(uploaded), pending or 0, max(0, (total_saved or 0) - len(saved)))


async def source_summary(repo: TenantRepo, job_id: uuid.UUID) -> dict[str, Any]:
    sources = await load_sources(repo, job_id)
    resume_ids = set(sources.database.values()) | set(sources.uploads.values())
    ready = set(
        (
            await repo.session.execute(
                select(Resume.id).where(
                    Resume.company_id == repo.company_id,
                    Resume.id.in_(resume_ids),
                    Resume.status == "ready",
                    Resume.candidate_id.in_(consenting_candidates(repo.company_id)),
                )
            )
        ).scalars()
    )

    def counts(source: ResumeSource) -> dict[str, int]:
        values = sources.selected(source)
        available = sum(resume_id in ready for resume_id in values.values())
        return {"total": len(values), "screenable": available, "blocked": len(values) - available}

    return {
        "database": counts("database"),
        "uploads": counts("uploads"),
        "both": counts("both"),
        "pending_uploads": sources.pending_uploads,
        "database_excluded": sources.database_excluded,
    }
