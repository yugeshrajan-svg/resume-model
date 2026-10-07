"""Explicitly enabled public applications and single-use interview booking links."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from backend.api.deps import Services, require, services
from backend.api.routes_recruiting import ActivityIn, create_activity
from backend.db import TenantRepo
from backend.models import Candidate, Company, HiringPlan, HRRecord, Job, User
from backend.security import Principal
from backend.services.common import ServiceError, audit
from backend.services.resumes import RegisterItem, register_resume, validate_upload_request
from backend.storage import blob_name_for

router = APIRouter(prefix="/v1", tags=["candidate portal"])


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: uuid.UUID
    confirmed: bool


@router.post("/portal/jobs", status_code=201)
async def publish(
    body: PublishIn,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
):
    if not body.confirmed:
        raise ServiceError("Review and confirm the job description before enabling its public page", status=422)
    async with svc.tenant(principal) as repo:
        job = await repo.get(Job, body.job_id, for_update=True)
        existing = await repo.find(
            HRRecord, HRRecord.kind == "public_job", HRRecord.job_id == job.id, HRRecord.status == "published"
        )
        if existing:
            return {"id": existing.id, "kind": existing.kind, "title": existing.title, "status": existing.status}
        row = repo.add(
            HRRecord(
                kind="public_job",
                title=job.title,
                status="published",
                job_id=job.id,
                data={"title": job.title, "jd_text": job.jd_text},
                history=[],
                created_by=principal.user_id,
            )
        )
        await repo.flush()
        audit(repo, principal, "job.public_page_enabled", "job", job.id, after={"page_id": str(row.id)})
        return {"id": row.id, "kind": row.kind, "title": row.title, "status": row.status}


@router.get("/portal/links")
async def links(
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
    offset: int = 0,
):
    if offset < 0:
        raise ServiceError("Offset must be non-negative", status=422)
    async with svc.tenant(principal) as repo:
        rows = (
            (
                await repo.session.execute(
                    repo.select(HRRecord, HRRecord.kind.in_(["public_job", "public_booking"]))
                    .order_by(HRRecord.created_at.desc())
                    .limit(25)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "kind": row.kind,
                "title": row.title,
                "status": row.status,
                "data": row.data,
                "candidate_id": row.candidate_id,
                "job_id": row.job_id,
            }
            for row in rows
        ]


@router.post("/portal/links/{link_id}/close")
async def close(
    link_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
):
    async with svc.tenant(principal) as repo:
        row = await repo.get(HRRecord, link_id, for_update=True)
        if row.kind not in {"public_job", "public_booking"}:
            raise ServiceError("Choose a portal link", status=422)
        row.status = "closed"
        audit(
            repo,
            principal,
            "portal.link_closed",
            "candidate" if row.candidate_id else "job",
            row.candidate_id or row.job_id,
            after={"link_id": str(row.id)},
        )
        return {"status": "closed"}


async def company_for_link(svc, link_id, kind):
    async with svc.db.system_session() as session:
        row = (
            await session.execute(select(HRRecord.company_id).where(HRRecord.id == link_id, HRRecord.kind == kind))
        ).scalar_one_or_none()
        if row is None:
            raise ServiceError("This link is unavailable", status=404)
        return row


def usable(row):
    if row.status not in {"published", "open"}:
        raise ServiceError("This link is closed or already used", status=410)
    if row.data.get("expires_at") and datetime.fromisoformat(row.data["expires_at"]) <= datetime.now(UTC):
        raise ServiceError("This booking link has expired", status=410)


@router.get("/public/jobs/{link_id}")
async def public_job(link_id: uuid.UUID, svc: Annotated[Services, Depends(services)]):
    company = await company_for_link(svc, link_id, "public_job")
    async with svc.db.tenant_session(company) as session:
        repo = TenantRepo(session, company)
        row = await repo.get(HRRecord, link_id)
        usable(row)
        name = (await session.execute(select(Company.name).where(Company.id == company))).scalar_one()
        return {"title": row.data["title"], "jd_text": row.data["jd_text"], "company": name}


@router.post("/public/jobs/{link_id}/apply", status_code=202)
async def apply(link_id: uuid.UUID, request: Request, svc: Annotated[Services, Depends(services)]):
    company = await company_for_link(svc, link_id, "public_job")
    if request.headers.get("x-candidate-consent") != "yes":
        raise ServiceError("Confirm consent to process your application for this job", status=422)
    filename = unquote(request.headers.get("x-filename", ""))
    if not filename or len(filename) > 200 or "/" in filename or "\\" in filename:
        raise ServiceError("Use a PDF, DOCX or TXT resume filename", status=422)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 20 * 1024 * 1024:
            raise ServiceError("Resume must be at most 20 MB", status=413)
    validate_upload_request(filename, len(data), 20 * 1024 * 1024)
    async with svc.db.tenant_session(company) as session:
        repo = TenantRepo(session, company)
        row = await repo.get(HRRecord, link_id, for_update=True)
        usable(row)
        now = datetime.now(UTC)
        day = now.date().isoformat()
        daily = int(row.data.get("daily_count", 0)) if row.data.get("count_day") == day else 0
        if daily >= 1000:
            raise ServiceError("Applications are temporarily limited; try again tomorrow", status=429)
        name = blob_name_for(company, filename)
        await svc.storage.put(name, bytes(data))
        try:
            await register_resume(
                repo,
                "public:application",
                svc.storage,
                svc.queue,
                RegisterItem(
                    name,
                    filename,
                    row.job_id,
                    {
                        "source": "Candidate submitted public application",
                        "purpose": "Recruitment for " + row.title[:260],
                        "scope": "job",
                        "granted_at": now.isoformat(),
                    },
                    source_type="public_application",
                ),
            )
        except Exception:
            await svc.storage.delete(name)
            raise
        row.data = {**row.data, "count_day": day, "daily_count": daily + 1}
        return {"received": True}


class SlotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def valid(self):
        if (
            self.start.utcoffset() is None
            or self.end.utcoffset() is None
            or not datetime.now(UTC) < self.start < self.end
            or self.end - self.start > timedelta(hours=4)
            or self.end > datetime.now(UTC) + timedelta(days=14)
        ):
            raise ValueError("Offer future slots within 14 days, up to four hours each, with a timezone")
        return self


class BookingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: uuid.UUID
    job_id: uuid.UUID
    assigned_to: uuid.UUID
    slots: list[SlotIn] = Field(min_length=1, max_length=20)
    location: str = Field(default="", max_length=1000)
    round_id: str | None = None
    plan_version: int | None = None


@router.post("/portal/booking-links", status_code=201)
async def booking_link(
    body: BookingIn,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
):
    async with svc.tenant(principal) as repo:
        candidate = await repo.get(Candidate, body.candidate_id, for_update=True)
        job = await repo.get(Job, body.job_id)
        owner = await repo.get(User, body.assigned_to)
        if not owner.is_active or owner.role == "employee":
            raise ServiceError("Choose an active interviewer", status=422)
        plans = await repo.list(HiringPlan, HiringPlan.job_id == job.id, order_by=[HiringPlan.version.desc()], limit=1)
        if plans and not body.round_id:
            raise ServiceError("Choose an interview round", status=422)
        if body.round_id:
            plan = await repo.find(HiringPlan, HiringPlan.job_id == job.id, HiringPlan.version == body.plan_version)
            if not plan or not any(r["id"] == body.round_id for r in plan.rounds):
                raise ServiceError("Choose a valid job interview round", status=422)
        row = repo.add(
            HRRecord(
                kind="public_booking",
                title="Interview · " + job.title,
                status="open",
                candidate_id=candidate.id,
                job_id=job.id,
                assigned_to=owner.id,
                created_by=principal.user_id,
                history=[],
                data={
                    "slots": [s.model_dump(mode="json") for s in body.slots],
                    "expires_at": max(s.end for s in body.slots).isoformat(),
                    "location": body.location,
                    "round_id": body.round_id,
                    "plan_version": body.plan_version,
                },
            )
        )
        candidate.last_activity_at = datetime.now(UTC)
        await repo.flush()
        audit(repo, principal, "booking.link_created", "candidate", candidate.id, after={"link_id": str(row.id)})
        return {"id": row.id, "kind": row.kind, "title": row.title, "status": row.status}


@router.get("/public/book/{link_id}")
async def public_booking(link_id: uuid.UUID, svc: Annotated[Services, Depends(services)]):
    company = await company_for_link(svc, link_id, "public_booking")
    async with svc.db.tenant_session(company) as session:
        row = await TenantRepo(session, company).get(HRRecord, link_id)
        usable(row)
        return {
            "title": row.title,
            "location": row.data["location"],
            "slots": [
                {"index": i, **slot}
                for i, slot in enumerate(row.data["slots"])
                if datetime.fromisoformat(slot["start"]) > datetime.now(UTC)
            ],
        }


class ChooseSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0, le=19)


@router.post("/public/book/{link_id}")
async def book(link_id: uuid.UUID, body: ChooseSlot, svc: Annotated[Services, Depends(services)]):
    company = await company_for_link(svc, link_id, "public_booking")
    async with svc.db.tenant_session(company) as session:
        repo = TenantRepo(session, company)
        initial = await repo.get(HRRecord, link_id)
        await repo.get(Candidate, initial.candidate_id, for_update=True)
        row = await repo.get(HRRecord, link_id, for_update=True)
        usable(row)
        if body.index >= len(row.data["slots"]):
            raise ServiceError("Choose one of the offered slots", status=422)
        slot = row.data["slots"][body.index]
        if datetime.fromisoformat(slot["start"]) <= datetime.now(UTC):
            raise ServiceError("This slot has passed", status=409)
        creator = await repo.get(User, row.created_by)
        owner = await repo.get(User, row.assigned_to)
        if (
            not creator.is_active
            or creator.role not in {"admin", "recruiter"}
            or not owner.is_active
            or owner.role == "employee"
        ):
            raise ServiceError("This booking link is unavailable", status=410)
        actor = Principal(creator.id, company, creator.role)
        record = await create_activity(
            repo,
            actor,
            ActivityIn(
                kind="interview",
                title=row.title,
                candidate_id=row.candidate_id,
                job_id=row.job_id,
                assigned_to=row.assigned_to,
                due_at=slot["start"],
                end_at=slot["end"],
                location=row.data["location"],
                round_id=row.data["round_id"],
                plan_version=row.data["plan_version"],
            ),
            public_actor="public:booking",
        )
        row.status = "booked"
        row.data = {**row.data, "selected_index": body.index, "interview_id": str(record.id)}
        return {"booked": True, "start": record.due_at, "end": record.end_at}
