"""Jobs, versioned requirements, uploads and resume registration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from azure.core.exceptions import AzureError
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.api.deps import Services, require, services
from backend.api.schemas import (
    BulkItemOut,
    BulkRegisterIn,
    ConfirmIn,
    JobIn,
    JobOut,
    RegisterIn,
    RegisterOut,
    RequirementsIn,
    RequirementsVersionOut,
    ResumeOut,
    SasIn,
    SasOut,
)
from backend.db import NotFound, TenantRepo
from backend.models import Job, JobRequirementsVersion, Resume
from backend.security import Principal
from backend.services.common import ServiceError, audit
from backend.services.pool_matching import enqueue_match
from backend.services.resumes import RegisterItem, register_resume, reprocess_resume, validate_upload_request
from backend.storage import blob_name_for
from engine.config import get_settings as get_engine_settings
from engine.extraction import ExtractionFailed, extract_job_requirements
from engine.llm import get_llm

router = APIRouter(prefix="/v1", tags=["jobs"])


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobIn,
    principal: Annotated[Principal, Depends(require("jobs:write"))],
    svc: Annotated[Services, Depends(services)],
) -> Job:
    async with svc.tenant(principal) as repo:
        job = repo.add(Job(id=uuid.uuid4(), title=body.title, jd_text=body.jd_text, created_by=principal.user_id))
        await repo.flush()
        audit(repo, principal, "job.created", "job", job.id, after={"title": body.title})
        return job


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    principal: Annotated[Principal, Depends(require("jobs:read"))],
    svc: Annotated[Services, Depends(services)],
    q: str | None = None,
) -> list[Job]:
    async with svc.tenant(principal) as repo:
        where = [Job.title.ilike(f"%{q}%")] if q else []
        return await repo.list(Job, *where, order_by=[Job.created_at.desc()])


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("jobs:read"))],
    svc: Annotated[Services, Depends(services)],
) -> Job:
    async with svc.tenant(principal) as repo:
        return await repo.get(Job, job_id)


async def _next_version(repo: TenantRepo, job_id: uuid.UUID) -> int:
    latest = (
        await repo.session.execute(
            select(func.max(JobRequirementsVersion.version)).where(
                JobRequirementsVersion.company_id == repo.company_id, JobRequirementsVersion.job_id == job_id
            )
        )
    ).scalar()
    return (latest or 0) + 1


@router.post("/jobs/{job_id}/analyze", response_model=RequirementsVersionOut, status_code=status.HTTP_201_CREATED)
async def analyze_job(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("jobs:write"))],
    svc: Annotated[Services, Depends(services)],
) -> JobRequirementsVersion:
    """Extract requirements from the JD into a new, unconfirmed version for HR review."""
    async with svc.tenant(principal) as repo:
        jd_text = (await repo.get(Job, job_id)).jd_text
    settings = get_engine_settings()
    try:
        analysis = await asyncio.to_thread(extract_job_requirements, jd_text, get_llm(settings), settings)
    except ExtractionFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Requirement extraction failed: {exc}") from exc
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id, for_update=True)
        version = repo.add(
            JobRequirementsVersion(
                job_id=job_id,
                version=await _next_version(repo, job_id),
                requirements=analysis.requirements.model_dump(mode="json", exclude={"requirements_confirmed"}),
                source="analysis",
                analysis_meta={
                    "method": analysis.method,
                    "prompt_version": analysis.prompt_version,
                    "model": analysis.model,
                    "evidence_drop_rates": {k: v.drop_rate for k, v in analysis.evidence_stats.items()},
                },
                created_by=principal.user_id,
            )
        )
        await repo.flush()
        audit(repo, principal, "requirements.analyzed", "job", job_id, after={"version": version.version})
        if analysis.usage:
            from backend.services.common import record_usage

            record_usage(
                repo,
                "llm",
                prompt_tokens=analysis.usage.prompt_tokens,
                completion_tokens=analysis.usage.completion_tokens,
                cost_usd=analysis.usage.cost_usd,
            )
        return version


@router.get("/jobs/{job_id}/requirements", response_model=list[RequirementsVersionOut])
async def list_requirements(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("jobs:read"))],
    svc: Annotated[Services, Depends(services)],
) -> list[JobRequirementsVersion]:
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id)
        return await repo.list(
            JobRequirementsVersion, JobRequirementsVersion.job_id == job_id, order_by=[JobRequirementsVersion.version]
        )


@router.put("/jobs/{job_id}/requirements", response_model=RequirementsVersionOut, status_code=status.HTTP_201_CREATED)
async def edit_requirements(
    job_id: uuid.UUID,
    body: RequirementsIn,
    principal: Annotated[Principal, Depends(require("jobs:write"))],
    svc: Annotated[Services, Depends(services)],
) -> JobRequirementsVersion:
    """Edits never change an existing version: they create the next one (unconfirmed)."""
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id, for_update=True)
        version = repo.add(
            JobRequirementsVersion(
                job_id=job_id,
                version=await _next_version(repo, job_id),
                requirements=body.to_engine(),
                source="manual",
                created_by=principal.user_id,
            )
        )
        await repo.flush()
        audit(repo, principal, "requirements.edited", "job", job_id, after={"version": version.version})
        return version


@router.post("/jobs/{job_id}/requirements/confirm", response_model=RequirementsVersionOut)
async def confirm_requirements(
    job_id: uuid.UUID,
    body: ConfirmIn,
    principal: Annotated[Principal, Depends(require("jobs:write"))],
    svc: Annotated[Services, Depends(services)],
) -> JobRequirementsVersion:
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id, for_update=True)
        version = await repo.find(
            JobRequirementsVersion,
            JobRequirementsVersion.job_id == job_id,
            JobRequirementsVersion.version == body.version,
        )
        if version is None:
            raise NotFound(f"requirements version {body.version} not found")
        if version.confirmed_at is not None:
            raise ServiceError(f"Version {body.version} is already confirmed (confirmed versions are immutable)")
        version.confirmed_by = principal.user_id
        version.confirmed_at = datetime.now(UTC)
        audit(repo, principal, "requirements.confirmed", "job", job_id, after={"version": body.version})
        # Suggest eligible talent-pool candidates for this version in the background; nothing is applied.
        await enqueue_match(repo, svc.queue, version)
        return version


@router.post("/uploads/sas", response_model=SasOut)
async def upload_sas(
    body: SasIn,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
    svc: Annotated[Services, Depends(services)],
) -> SasOut:
    """A short-lived, write-only URL for one blob under the caller's company prefix."""
    validate_upload_request(body.filename, body.size, get_engine_settings().max_file_bytes)
    blob_name = blob_name_for(principal.company_id, body.filename)
    expires_at = datetime.now(UTC) + timedelta(seconds=svc.settings.sas_ttl_s)
    return SasOut(
        blob_name=blob_name,
        upload_url=svc.storage.upload_url(blob_name, expires_at),
        expires_at=expires_at,
        headers={"x-ms-blob-type": "BlockBlob"},
    )


def _item(body: RegisterIn) -> RegisterItem:
    return RegisterItem(
        blob_name=body.blob_name,
        filename=body.filename,
        job_id=body.job_id,
        consent=body.consent.model_dump(mode="json") if body.consent else None,
        source_type="upload" if body.job_id else "talent_pool",
    )


@router.post("/resumes/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterIn,
    response: Response,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
    svc: Annotated[Services, Depends(services)],
) -> RegisterOut:
    async with svc.tenant(principal) as repo:
        resume, created = await register_resume(repo, principal, svc.storage, svc.queue, _item(body))
        response.status_code = 201 if created else 200
        return RegisterOut(resume=ResumeOut.model_validate(resume), created=created)


@router.post("/resumes/register-bulk", response_model=list[BulkItemOut])
async def register_bulk(
    body: BulkRegisterIn,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
    svc: Annotated[Services, Depends(services)],
) -> list[BulkItemOut]:
    """Each item is registered in its own transaction; one bad file does not fail the batch."""
    if len(body.items) > svc.settings.max_bulk_register:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, f"At most {svc.settings.max_bulk_register} items")
    out: list[BulkItemOut] = []
    for item in body.items:
        try:
            async with svc.tenant(principal) as repo:
                resume, created = await register_resume(repo, principal, svc.storage, svc.queue, _item(item))
                registered = BulkItemOut(
                    blob_name=item.blob_name,
                    resume=ResumeOut.model_validate(resume),
                    created=created,
                    status_code=201 if created else 200,
                )
            out.append(registered)
        except (ServiceError, NotFound) as exc:
            code = exc.status if isinstance(exc, ServiceError) else 404
            out.append(BulkItemOut(blob_name=item.blob_name, error=str(exc), status_code=code))
        except IntegrityError:
            out.append(
                BulkItemOut(
                    blob_name=item.blob_name, error="Registration conflicts with existing data", status_code=409
                )
            )
        except (SQLAlchemyError, AzureError):
            out.append(
                BulkItemOut(blob_name=item.blob_name, error="Upload service temporarily unavailable", status_code=503)
            )
    return out


@router.post("/resumes/{resume_id}/reprocess", response_model=ResumeOut, status_code=status.HTTP_202_ACCEPTED)
async def reprocess(
    resume_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
    svc: Annotated[Services, Depends(services)],
) -> Resume:
    async with svc.tenant(principal) as repo:
        return await reprocess_resume(repo, principal, svc.queue, resume_id)


@router.get("/resumes/{resume_id}", response_model=ResumeOut)
async def get_resume(
    resume_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
) -> ResumeOut:
    async with svc.tenant(principal) as repo:
        out = ResumeOut.model_validate(await repo.get(Resume, resume_id))
    # Filenames often contain the candidate's name: shown only to roles with contact access.
    return out if principal.can("contacts:read") else out.model_copy(update={"filename": None})
