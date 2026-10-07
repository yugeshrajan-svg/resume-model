"""Talent pool: semantic search across all candidates, re-indexing, and adding a found candidate to a job."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import func, or_, select

from backend.api.deps import Services, require, services
from backend.api.schemas import AddManyToJobIn, AddToJobIn, TalentSearchIn
from backend.db import NotFound
from backend.models import CandidateContact, Resume
from backend.security import Principal
from backend.services.common import ServiceError
from backend.services.pool_matching import pool_matches, refresh
from backend.services.screening_sources import consenting_candidates
from backend.services.talent_pool import add_to_job, reindex, search

router = APIRouter(prefix="/v1", tags=["talent pool"])


@router.get("/talent-pool/resumes")
async def saved_resumes(
    principal: Annotated[Principal, Depends(require("contacts:read"))],
    svc: Annotated[Services, Depends(services)],
    q: Annotated[str, Query(max_length=300)] = "",
    view: Annotated[str, Query(pattern="^(all|consent|processing|attention)$")] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Browse stored resume versions even before indexing or screening, scoped to the caller's company."""
    async with svc.tenant(principal) as repo:
        active = Resume.candidate_id.in_(consenting_candidates(repo.company_id))
        pool = Resume.candidate_id.in_(consenting_candidates(repo.company_id, pool=True))
        processing = Resume.status.in_(["registered", "processing"])
        attention = Resume.status.in_(["failed", "needs_ocr", "needs_reprocess"])
        missing = Resume.candidate_id.is_not(None) & ~pool
        summary_row = (
            await repo.session.execute(
                select(
                    func.count(),
                    func.count().filter(processing),
                    func.count().filter(attention),
                    func.count().filter(missing),
                )
                .select_from(Resume)
                .where(Resume.company_id == repo.company_id)
            )
        ).one()
        summary = dict(zip(["total", "processing", "attention", "needs_consent"], summary_row, strict=True))
        filters = [Resume.company_id == repo.company_id]
        if q.strip():
            term = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            filters.append(
                or_(Resume.filename.ilike(term, escape="\\"), CandidateContact.name.ilike(term, escape="\\"))
            )
        if view != "all":
            filters.append({"consent": missing, "processing": processing, "attention": attention}[view])
        base = (
            select(Resume, CandidateContact.name, active.label("consented"), pool.label("pool_consent"))
            .outerjoin(
                CandidateContact,
                (CandidateContact.candidate_id == Resume.candidate_id)
                & (CandidateContact.company_id == repo.company_id),
            )
            .where(*filters)
        )
        total = (await repo.session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        rows = (
            await repo.session.execute(base.order_by(Resume.created_at.desc(), Resume.id).limit(limit).offset(offset))
        ).all()
        return {
            "summary": summary,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": [
                {
                    "resume_id": str(resume.id),
                    "candidate_id": str(resume.candidate_id) if resume.candidate_id else None,
                    "filename": resume.filename,
                    "name": name,
                    "status": resume.status,
                    "version": resume.version,
                    "created_at": resume.created_at.isoformat(),
                    "has_consent": bool(consented),
                    "has_pool_consent": bool(pool_consent),
                }
                for resume, name, consented, pool_consent in rows
            ],
        }


@router.post("/talent-pool/search")
async def search_talent_pool(
    body: TalentSearchIn,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    """Candidates whose resumes are most similar to the query or job description, best first.

    ``similarity`` is semantic closeness (cosine, -1..1), not a screening score: screen candidates for that.
    Names are included only for roles with contact access.
    """
    async with svc.tenant(principal) as repo:
        result = await search(repo, principal, query=body.query, job_id=body.job_id, limit=body.limit)
        if principal.can("contacts:read") and result["results"]:
            ids = [uuid.UUID(item["candidate_id"]) for item in result["results"]]
            names = {
                c.candidate_id: c.name
                for c in await repo.list(CandidateContact, CandidateContact.candidate_id.in_(ids))
            }
            for item in result["results"]:
                item["name"] = names.get(uuid.UUID(item["candidate_id"]))
        return result


@router.post("/talent-pool/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_talent_pool(
    principal: Annotated[Principal, Depends(require("company:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, int]:
    """Queue search vectors for processed resumes that have none for the current embedding model."""
    async with svc.tenant(principal) as repo:
        return {"queued": await reindex(repo, principal, svc.queue)}


@router.post("/jobs/{job_id}/applications", status_code=status.HTTP_201_CREATED)
async def add_candidate_to_job(
    job_id: uuid.UUID,
    body: AddToJobIn,
    response: Response,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    """Apply an existing candidate (e.g. from a talent-pool search) to a job with their latest processed resume.

    The next screening run of the job includes them (consent is checked there, as for uploads). Returns 200 with
    the existing application if they already applied.
    """
    async with svc.tenant(principal) as repo:
        app, created = await add_to_job(repo, principal, job_id, body.candidate_id)
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return {
            "application_id": str(app.id),
            "job_id": str(app.job_id),
            "candidate_id": str(app.candidate_id),
            "resume_id": str(app.resume_id),
            "stage": app.stage,
            "created": created,
        }


@router.post("/jobs/{job_id}/applications/bulk")
async def add_candidates_to_job(
    job_id: uuid.UUID,
    body: AddManyToJobIn,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    """Apply several existing candidates (e.g. all eligible pool matches) to a job; each is reported separately."""
    added, existing, failed = [], [], []
    async with svc.tenant(principal) as repo:
        for candidate_id in dict.fromkeys(body.candidate_ids):
            try:
                app, created = await add_to_job(repo, principal, job_id, candidate_id)
            except NotFound:
                failed.append({"candidate_id": str(candidate_id), "error": "Resource not found"})
            except ServiceError as exc:
                failed.append({"candidate_id": str(candidate_id), "error": str(exc)})
            else:
                (added if created else existing).append(str(app.candidate_id))
    return {"job_id": str(job_id), "added": added, "already_applied": existing, "failed": failed}


@router.get("/jobs/{job_id}/pool-matches")
async def job_pool_matches(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """Talent-pool candidates checked against a confirmed requirements version (default: the latest).

    Only candidates with an active talent-pool consent are listed. ``eligible`` passed every knockout rule of that
    version, ordered by semantic closeness to the job; ``ineligible`` lists the failed rules. Suggestions only:
    add candidates to the job and run a screening for scores.
    """
    async with svc.tenant(principal) as repo:
        return await pool_matches(repo, principal, job_id, version)


@router.post("/jobs/{job_id}/pool-matches/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_pool_matches(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("screening:run"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, int]:
    """Check the talent pool again for the latest confirmed version (e.g. after new candidates joined)."""
    async with svc.tenant(principal) as repo:
        return {"requirements_version": await refresh(repo, principal, svc.queue, job_id)}
