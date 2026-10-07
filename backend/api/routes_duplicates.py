"""Human review and conservative consolidation; overlapping applications are never discarded."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, or_, select, text, update

from backend.api.deps import Services, require, services
from backend.models import (
    Application,
    Candidate,
    CandidateAlias,
    CandidateContact,
    CandidateEmbedding,
    Consent,
    HRRecord,
    PoolMatch,
    QueueJob,
    RecruitingActivity,
    Resume,
    ScreeningInput,
    ScreeningResult,
    ScreeningRun,
)
from backend.security import Principal
from backend.services.common import ServiceError, audit

router = APIRouter(prefix="/v1", tags=["duplicate review"])


@router.get("/talent-pool/duplicates")
async def duplicates(
    principal: Annotated[Principal, Depends(require("contacts:read"))],
    svc: Annotated[Services, Depends(services)],
    q: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
):
    async with svc.tenant(principal) as repo:
        name = func.lower(func.trim(CandidateContact.name))
        query = select(name.label("name"), func.array_agg(CandidateContact.candidate_id).label("ids")).where(
            CandidateContact.company_id == repo.company_id, name != ""
        )
        if q.strip():
            query = query.where(name.contains(q.strip().lower(), autoescape=True))
        query = query.group_by(name).having(func.count() > 1)
        total = (await repo.session.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        groups = (await repo.session.execute(query.order_by(name).limit(25).offset(offset))).all()
        ids = [cid for _, group in groups for cid in group[:20]]
        contacts = (
            {row.candidate_id: row for row in await repo.list(CandidateContact, CandidateContact.candidate_id.in_(ids))}
            if ids
            else {}
        )
        return {
            "total": total,
            "items": [
                {
                    "name": name,
                    "count": len(group),
                    "profiles": [
                        {
                            "id": str(cid),
                            "name": contacts[cid].name,
                            "email": contacts[cid].email,
                            "phone": contacts[cid].phone,
                        }
                        for cid in group[:20]
                    ],
                }
                for name, group in groups
            ],
        }


class MergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: uuid.UUID
    target_id: uuid.UUID
    reason: str = Field(min_length=10, max_length=2000)
    confirmed: bool = False


async def preview(repo, body):
    if body.source_id == body.target_id:
        raise ServiceError("Choose two different profiles", status=422)
    await repo.get(Candidate, body.source_id)
    await repo.get(Candidate, body.target_id)
    source_apps = await repo.list(Application, Application.candidate_id == body.source_id)
    target_apps = await repo.list(Application, Application.candidate_id == body.target_id)
    resumes = await repo.list(Resume, Resume.candidate_id.in_([body.source_id, body.target_id]))
    shared = {a.job_id for a in source_apps} & {a.job_id for a in target_apps}
    active_run = await repo.find(
        ScreeningRun,
        ScreeningRun.job_id.in_([a.job_id for a in source_apps + target_apps]),
        ScreeningRun.status.in_(["queued", "running"]),
    )
    refs = [
        str(body.source_id),
        str(body.target_id),
        *(str(r.id) for r in resumes),
        *(str(a.id) for a in source_apps + target_apps),
    ]
    active = await repo.find(
        QueueJob,
        QueueJob.status.in_(["running", "queued"]),
        or_(*(QueueJob.payload[key].astext.in_(refs) for key in ["candidate_id", "resume_id", "application_id"])),
    )
    return {
        "source_id": body.source_id,
        "target_id": body.target_id,
        "source_resumes": sum(r.candidate_id == body.source_id for r in resumes),
        "source_applications": len(source_apps),
        "conflicting_jobs": list(shared),
        "active_processing": bool(active or active_run),
        "unfinished_resumes": any(r.status != "ready" for r in resumes),
        "can_merge": not shared and not active and not active_run and all(r.status == "ready" for r in resumes),
    }


@router.post("/candidates/merge/preview")
async def preview_merge(
    body: MergeIn,
    principal: Annotated[Principal, Depends(require("candidates:delete"))],
    svc: Annotated[Services, Depends(services)],
):
    async with svc.tenant(principal) as repo:
        return await preview(repo, body)


@router.post("/candidates/merge")
async def merge(
    body: MergeIn,
    principal: Annotated[Principal, Depends(require("candidates:delete"))],
    svc: Annotated[Services, Depends(services)],
):
    if not body.confirmed:
        raise ServiceError("Confirm that the profiles represent the same person", status=422)
    async with svc.tenant(principal) as repo:
        initial_contacts = await repo.list(
            CandidateContact, CandidateContact.candidate_id.in_([body.source_id, body.target_id])
        )
        aliases = await repo.list(CandidateAlias, CandidateAlias.candidate_id.in_([body.source_id, body.target_id]))
        keys = {h for row in initial_contacts for h in (row.email_hash, row.phone_hash) if h} | {
            a.contact_hash for a in aliases
        }
        for key in sorted(keys):
            await repo.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"{repo.company_id}:{key}"}
            )
        for candidate_id in sorted([body.source_id, body.target_id]):
            await repo.get(Candidate, candidate_id, for_update=True)
        summary = await preview(repo, body)
        if not summary["can_merge"]:
            raise ServiceError(
                "Consolidation is blocked by overlapping job applications or unfinished processing. "
                "Keep both profiles to preserve history.",
                status=409,
                details=summary,
            )
        source = await repo.find(CandidateContact, CandidateContact.candidate_id == body.source_id)
        target = await repo.find(CandidateContact, CandidateContact.candidate_id == body.target_id)
        current_keys = {h for row in (source, target) if row for h in (row.email_hash, row.phone_hash) if h}
        if not current_keys.issubset(keys):
            raise ServiceError("Contact identities changed. Review both profiles again.", status=409)
        for contact in (source, target):
            if not contact:
                continue
            for kind, value in (("email", contact.email_hash), ("phone", contact.phone_hash)):
                if value and not await repo.find(
                    CandidateAlias, CandidateAlias.kind == kind, CandidateAlias.contact_hash == value
                ):
                    repo.add(CandidateAlias(candidate_id=body.target_id, kind=kind, contact_hash=value))
        if source and target:
            for field in ("name", "email", "phone", "email_hash", "phone_hash"):
                if not getattr(target, field):
                    setattr(target, field, getattr(source, field))
        elif source:
            source.candidate_id = body.target_id
        # Derived suggestions can be recomputed; conflicting historical applications were rejected above.
        await repo.session.execute(
            delete(PoolMatch).where(PoolMatch.company_id == repo.company_id, PoolMatch.candidate_id == body.source_id)
        )
        for model in (
            Resume,
            Application,
            ScreeningInput,
            ScreeningResult,
            RecruitingActivity,
            Consent,
            CandidateEmbedding,
            CandidateAlias,
            HRRecord,
        ):
            await repo.session.execute(
                update(model)
                .where(model.company_id == repo.company_id, model.candidate_id == body.source_id)
                .values(candidate_id=body.target_id)
            )
        target_candidate = await repo.get(Candidate, body.target_id)
        target_candidate.last_activity_at = datetime.now(UTC)
        await repo.flush()
        await repo.session.execute(
            delete(Candidate).where(Candidate.company_id == repo.company_id, Candidate.id == body.source_id)
        )
        audit(
            repo,
            principal,
            "candidate.consolidated",
            "candidate",
            body.target_id,
            after={
                "source_id": str(body.source_id),
                "resumes": summary["source_resumes"],
                "applications": summary["source_applications"],
            },
        )
        repo.add(
            RecruitingActivity(
                kind="note",
                title="Profile consolidation",
                notes=body.reason,
                candidate_id=body.target_id,
                status="completed",
                created_by=principal.user_id,
                assigned_to=principal.user_id,
                details={"source_id": str(body.source_id)},
            )
        )
        return {
            "candidate_id": body.target_id,
            "moved_resumes": summary["source_resumes"],
            "moved_applications": summary["source_applications"],
        }
