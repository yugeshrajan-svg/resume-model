import uuid
from typing import Annotated
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from backend.api.deps import Services, require, services
from backend.api.schemas import ConsentIn, RegisterOut, ResumeOut
from backend.models import Job, Resume
from backend.security import Principal
from backend.services import drive
from backend.services.common import ServiceError, audit
from backend.services.resumes import RegisterItem, register_resume, validate_upload_request
from backend.storage import blob_name_for
from engine.config import get_settings

router = APIRouter(prefix="/v1", tags=["resume files"])


class DrivePreviewIn(BaseModel):
    link: str = Field(min_length=10, max_length=2000)


class DriveImportIn(BaseModel):
    file_id: str = Field(min_length=10, max_length=200)
    filename: str = Field(min_length=1, max_length=300)
    job_id: uuid.UUID | None = None
    consent: ConsentIn | None = None


@router.post("/drive/preview")
async def preview_drive(
    body: DrivePreviewIn,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
) -> dict:
    try:
        return {"files": await drive.preview(body.link)}
    except httpx.HTTPError as exc:
        raise ServiceError("Could not reach Google Drive. Please retry.", status=502) from exc


@router.post("/drive/import", response_model=RegisterOut)
async def import_drive(
    body: DriveImportIn,
    principal: Annotated[Principal, Depends(require("resumes:write"))],
    svc: Annotated[Services, Depends(services)],
) -> RegisterOut:
    if body.job_id is not None:
        async with svc.tenant(principal) as repo:
            await repo.get(Job, body.job_id)
    limit = get_settings().max_file_bytes
    validate_upload_request(body.filename, 1, limit)
    try:
        data = await drive.download(body.file_id, limit)
    except httpx.HTTPError as exc:
        raise ServiceError("Could not download from Drive. Please retry.", status=502) from exc
    validate_upload_request(body.filename, len(data), limit)
    blob = blob_name_for(principal.company_id, body.filename)
    await svc.storage.put(blob, data)
    try:
        async with svc.tenant(principal) as repo:
            resume, created = await register_resume(
                repo,
                principal,
                svc.storage,
                svc.queue,
                RegisterItem(
                    blob, body.filename, body.job_id, body.consent.model_dump(mode="json") if body.consent else None, source_type="drive"
                ),
            )
            result = RegisterOut(resume=ResumeOut.model_validate(resume), created=created)
    except Exception:
        await svc.storage.delete(blob)
        raise
    return result


@router.get("/resumes/{resume_id}/download")
async def download_resume(
    resume_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("contacts:read"))],
    svc: Annotated[Services, Depends(services)],
) -> Response:
    async with svc.tenant(principal) as repo:
        resume = await repo.get(Resume, resume_id)
        data = await svc.storage.read(resume.blob_name)
        audit(repo, principal, "resume.downloaded", "resume", resume.id)
        return Response(
            data,
            media_type=resume.mime,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(resume.filename or 'resume', safe='')}",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
