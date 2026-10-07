"""Recruiting approvals, offer templates and new-hire handover."""

import html
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from backend.api.deps import Services, require, services
from backend.models import (
    Application,
    Candidate,
    CandidateContact,
    Company,
    HiringPlan,
    HRRecord,
    Job,
    RecruitingActivity,
    User,
)
from backend.security import Principal
from backend.services.common import ServiceError, audit
from backend.services.hr_catalog import APPROVAL_KINDS, ATS_KINDS, CATALOG, offer_matches, validate_data

router = APIRouter(prefix="/v1", tags=["HR workflows"])


class RoundIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    name: str = Field(min_length=2, max_length=100)
    criteria: list[str] = Field(min_length=1, max_length=12)


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    rounds: list[RoundIn] = Field(min_length=1, max_length=12)


@router.get("/jobs/{job_id}/hiring-plan")
async def get_plan(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("jobs:read"))],
    svc: Annotated[Services, Depends(services)],
):
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id)
        rows = await repo.list(HiringPlan, HiringPlan.job_id == job_id, order_by=[HiringPlan.version.desc()], limit=1)
        return {"version": rows[0].version if rows else 0, "rounds": rows[0].rounds if rows else []}


@router.put("/jobs/{job_id}/hiring-plan")
async def save_plan(
    job_id: uuid.UUID,
    body: PlanIn,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
):
    if len({r.id for r in body.rounds}) != len(body.rounds):
        raise ServiceError("Each round needs a unique ID", status=422)
    for row in body.rounds:
        if any(not c.strip() or len(c.strip()) > 100 for c in row.criteria) or len(
            {c.strip().lower() for c in row.criteria}
        ) != len(row.criteria):
            raise ServiceError("Use distinct criteria of 1–100 characters", status=422)
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id, for_update=True)
        version = (
            await repo.session.execute(
                select(func.coalesce(func.max(HiringPlan.version), 0)).where(
                    HiringPlan.company_id == repo.company_id, HiringPlan.job_id == job_id
                )
            )
        ).scalar_one()
        if version != body.expected_version:
            raise ServiceError("Another teammate changed the plan. Reload before saving.", status=409)
        row = repo.add(
            HiringPlan(
                job_id=job_id,
                version=version + 1,
                rounds=[r.model_dump() | {"criteria": [c.strip() for c in r.criteria]} for r in body.rounds],
                created_by=principal.user_id,
            )
        )
        await repo.flush()
        audit(repo, principal, "hiring_plan.created", "job", job_id, after={"version": row.version})
        return {"version": row.version, "rounds": row.rounds}


class RecordIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: str = Field(max_length=30)
    title: str = Field(min_length=2, max_length=300)
    data: dict = Field(default_factory=dict)
    assigned_to: uuid.UUID | None = None
    subject_user_id: uuid.UUID | None = None
    candidate_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    activity_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    expected_updated_at: datetime
    reason: str = Field(min_length=2, max_length=2000)
    completed_items: list[int] | None = None


class RecordEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=2, max_length=300)
    data: dict
    expected_updated_at: datetime
    reason: str = Field(min_length=2, max_length=2000)
    subject_user_id: uuid.UUID | None = None


def visible(principal, kind):
    if kind not in CATALOG:
        return False
    if principal.role == "admin":
        return True
    if kind in ATS_KINDS:
        return principal.role in {"recruiter", "hiring_manager"} and (
            principal.role != "hiring_manager" or kind == "requisition"
        )
    return False


async def accessible(repo, principal, record_id, lock=False):
    row = await repo.get(HRRecord, record_id, for_update=lock)
    if not visible(principal, row.kind):
        raise ServiceError("This record is not available to your role", status=403)
    return row


def out(row):
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "kind",
            "title",
            "status",
            "data",
            "history",
            "candidate_id",
            "job_id",
            "activity_id",
            "parent_id",
            "subject_user_id",
            "assigned_to",
            "created_by",
            "created_at",
            "updated_at",
        )
    }


@router.get("/hr/catalog")
async def catalog(principal: Annotated[Principal, Depends(require("hr:access"))]):
    return {key: value for key, value in CATALOG.items() if visible(principal, key)}


@router.get("/hr/team")
async def team(
    principal: Annotated[Principal, Depends(require("hr:access"))], svc: Annotated[Services, Depends(services)]
):
    async with svc.tenant(principal) as repo:
        users = await repo.list(User, User.is_active.is_(True), order_by=[User.email])
        return [
            {
                "id": str(u.id),
                "label": u.email if principal.role in {"admin", "recruiter"} else f"{u.role} {str(u.id)[:8]}",
                "role": u.role,
            }
            for u in users
        ]


@router.get("/hr/records")
async def records(
    principal: Annotated[Principal, Depends(require("hr:access"))],
    svc: Annotated[Services, Depends(services)],
    kind: str = "",
    q: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    status: str = "",
    mine: bool = False,
):
    async with svc.tenant(principal) as repo:
        kinds = [key for key in CATALOG if visible(principal, key)]
        filters = [HRRecord.kind.in_(kinds)]
        if kind:
            filters.append(HRRecord.kind == kind)
        if status:
            filters.append(HRRecord.status == status)
        if mine:
            filters.append(HRRecord.assigned_to == principal.user_id)
        if q.strip():
            filters.append(func.lower(HRRecord.title).contains(q.strip().lower(), autoescape=True))
        stmt = repo.select(HRRecord, *filters)
        total = (await repo.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (
            (
                await repo.session.execute(
                    stmt.order_by(HRRecord.updated_at.desc(), HRRecord.id).limit(25).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return {"total": total, "items": [out(r) for r in rows]}


@router.post("/hr/records", status_code=201)
async def create_record(
    body: RecordIn,
    principal: Annotated[Principal, Depends(require("hr:access"))],
    svc: Annotated[Services, Depends(services)],
):
    if not visible(principal, body.kind) or (principal.role == "hiring_manager"):
        raise ServiceError("Your role cannot create this workflow", status=403)
    data = validate_data(body.kind, body.data)
    if body.subject_user_id or body.parent_id:
        raise ServiceError("Hiring workflows do not use employee profile links", status=422)
    async with svc.tenant(principal) as repo:
        if body.assigned_to:
            owner = await repo.get(User, body.assigned_to)
            if not owner.is_active:
                raise ServiceError("Choose an active reviewer", status=422)
            if body.kind in APPROVAL_KINDS:
                if owner.id == principal.user_id:
                    raise ServiceError("Approval requires another team member", status=422)
                allowed_roles = (
                    {"admin", "recruiter", "hiring_manager"}
                    if body.kind == "requisition"
                    else {"admin", "recruiter"}
                    if body.kind == "offer_approval"
                    else {"admin"}
                )
                if owner.role not in allowed_roles:
                    raise ServiceError("Choose a reviewer with permission for this workflow", status=422)
        elif body.kind in APPROVAL_KINDS:
            raise ServiceError("Assign an approval reviewer", status=422)
        if body.candidate_id:
            candidate = await repo.get(Candidate, body.candidate_id, for_update=True)
            candidate.last_activity_at = datetime.now(UTC)
        if body.job_id:
            await repo.get(Job, body.job_id)
        if body.kind == "onboarding":
            hiring_filters = [Application.candidate_id == body.candidate_id, Application.stage == "hired"]
            if body.job_id:
                hiring_filters.append(Application.job_id == body.job_id)
            if not body.candidate_id or not await repo.find(Application, *hiring_filters):
                raise ServiceError("Record a hire before starting this handover", status=422)
            data["completed_items"] = []
        if body.kind == "offer_approval":
            if not body.activity_id:
                raise ServiceError("Choose an offer record", status=422)
            offer = await repo.get(RecruitingActivity, body.activity_id, for_update=True)
            if offer.kind != "offer" or offer.status != "open":
                raise ServiceError("Only open offers can be submitted for approval", status=422)
            body.candidate_id, body.job_id = offer.candidate_id, offer.job_id
            data["offer_snapshot"] = offer.details
            data["offer_version"] = offer.updated_at.isoformat()
            data["offer_title"], data["offer_notes"] = offer.title, offer.notes
        elif body.activity_id:
            raise ServiceError("This workflow does not use an offer link", status=422)
        row = repo.add(
            HRRecord(
                kind=body.kind,
                title=body.title,
                data=data,
                status="pending" if body.kind in APPROVAL_KINDS else "open",
                created_by=principal.user_id,
                assigned_to=body.assigned_to,
                subject_user_id=None,
                candidate_id=body.candidate_id,
                job_id=body.job_id,
                activity_id=body.activity_id,
                parent_id=body.parent_id,
                history=[],
            )
        )
        await repo.flush()
        audit(
            repo,
            principal,
            "hr.created",
            "candidate" if row.candidate_id else "hr_record",
            row.candidate_id or row.id,
            after={"record_id": str(row.id), "kind": row.kind},
        )
        return out(row)


@router.post("/hr/records/{record_id}/action")
async def action(
    record_id: uuid.UUID,
    body: ActionIn,
    principal: Annotated[Principal, Depends(require("hr:access"))],
    svc: Annotated[Services, Depends(services)],
):
    async with svc.tenant(principal) as repo:
        initial = await accessible(repo, principal, record_id)
        if initial.candidate_id:
            candidate = await repo.get(Candidate, initial.candidate_id, for_update=True)
            candidate.last_activity_at = datetime.now(UTC)
        row = await accessible(repo, principal, record_id, True)
        if body.expected_updated_at.utcoffset() is None or row.updated_at != body.expected_updated_at:
            raise ServiceError("The record changed. Refresh and try again.", status=409)
        previous = row.status
        if body.action in {"approve", "reject"}:
            if (
                row.kind not in APPROVAL_KINDS
                or row.status != "pending"
                or row.assigned_to != principal.user_id
                or row.created_by == principal.user_id
            ):
                raise ServiceError("Only the assigned reviewer can decide a pending request", status=403)
            if row.kind == "offer_approval":
                offer = await repo.get(RecruitingActivity, row.activity_id, for_update=True)
                if not offer_matches(offer, row) or offer.status != "open":
                    raise ServiceError("The offer changed; submit a fresh approval request", status=409)
            row.status = "approved" if body.action == "approve" else "rejected"
            if body.action == "approve" and row.kind == "requisition":
                job = repo.add(Job(title=row.data["job_title"], jd_text=row.data["jd_text"], created_by=row.created_by))
                await repo.flush()
                row.job_id = job.id
        elif body.action == "withdraw":
            if row.status != "pending" or row.created_by != principal.user_id:
                raise ServiceError("Only the requester can withdraw a pending request", status=403)
            row.status = "withdrawn"
        elif body.action == "checklist":
            if row.kind != "onboarding" or principal.role not in {"admin", "recruiter"}:
                raise ServiceError("This record has no editable handover checklist", status=403)
            tasks = [line for line in row.data["checklist"].splitlines() if line.strip()]
            done = body.completed_items or []
            if len(set(done)) != len(done) or any(i < 0 or i >= len(tasks) for i in done):
                raise ServiceError("Invalid checklist task", status=422)
            row.data = {**row.data, "completed_items": sorted(done)}
            row.status = "completed" if len(done) == len(tasks) else "open"
        elif body.action in {"complete", "archive"}:
            if (
                principal.role not in {"admin", "recruiter"}
                or (row.kind not in ATS_KINDS and principal.role != "admin")
                or row.kind in APPROVAL_KINDS
            ):
                raise ServiceError("This action is not available for your role", status=403)
            row.status = "completed" if body.action == "complete" else "archived"
        else:
            raise ServiceError("Choose a supported action", status=422)
        row.updated_at = datetime.now(UTC)
        row.history = [
            *row.history,
            {
                "actor_id": str(principal.user_id),
                "action": body.action,
                "previous_status": previous,
                "status": row.status,
                "reason": body.reason,
                "at": row.updated_at.isoformat(),
            },
        ]
        audit(
            repo,
            principal,
            "hr.action",
            "candidate" if row.candidate_id else "hr_record",
            row.candidate_id or row.id,
            before={"status": previous},
            after={"record_id": str(row.id), "status": row.status, "action": body.action},
        )
        await repo.flush()
        return out(row)


@router.put("/hr/records/{record_id}")
async def edit_record(
    record_id: uuid.UUID,
    body: RecordEditIn,
    principal: Annotated[Principal, Depends(require("hr:access"))],
    svc: Annotated[Services, Depends(services)],
):
    async with svc.tenant(principal) as repo:
        row = await accessible(repo, principal, record_id, True)
        if row.kind in APPROVAL_KINDS or row.status not in {"open", "active"} or row.data.get("blob_name"):
            raise ServiceError(
                "Decided requests and completed records cannot be edited. "
                "Withdraw a pending request and submit a new one.",
                status=422,
            )
        if principal.role != "admin" and row.created_by != principal.user_id:
            raise ServiceError("Only the creator or an administrator can edit this record", status=403)
        if body.expected_updated_at != row.updated_at:
            raise ServiceError("The record changed. Refresh before editing.", status=409)
        values = validate_data(row.kind, body.data)
        if row.kind == "onboarding":
            old_tasks = [t.strip() for t in row.data["checklist"].splitlines() if t.strip()]
            new_tasks = [t.strip() for t in values["checklist"].splitlines() if t.strip()]
            values["completed_items"] = row.data.get("completed_items", []) if old_tasks == new_tasks else []
        row.title, row.data, row.updated_at = body.title.strip(), values, datetime.now(UTC)
        row.history = [
            *row.history,
            {
                "actor_id": str(principal.user_id),
                "action": "edit",
                "status": row.status,
                "reason": body.reason,
                "at": row.updated_at.isoformat(),
            },
        ]
        audit(
            repo,
            principal,
            "hr.edited",
            "candidate" if row.candidate_id else "hr_record",
            row.candidate_id or row.id,
            after={"record_id": str(row.id), "fields": sorted(values)},
        )
        return out(row)


class LetterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: uuid.UUID
    expected_updated_at: datetime
    reviewed: bool


@router.post("/recruiting/offers/{offer_id}/letter")
async def letter(
    offer_id: uuid.UUID,
    body: LetterIn,
    principal: Annotated[Principal, Depends(require("contacts:read"))],
    svc: Annotated[Services, Depends(services)],
):
    if not body.reviewed:
        raise ServiceError("Review the template and offer details before downloading", status=422)
    async with svc.tenant(principal) as repo:
        offer = await repo.get(RecruitingActivity, offer_id)
        template = await repo.get(HRRecord, body.template_id)
        if (
            offer.kind != "offer"
            or offer.status not in {"open", "accepted"}
            or template.kind != "offer_template"
            or template.status != "open"
        ):
            raise ServiceError("Choose an offer and an active letter template", status=422)
        if offer.updated_at != body.expected_updated_at:
            raise ServiceError("Offer changed. Reload its details before exporting.", status=409)
        approvals = await repo.list(
            HRRecord,
            HRRecord.kind == "offer_approval",
            HRRecord.activity_id == offer.id,
            order_by=[HRRecord.created_at.desc()],
            limit=1,
        )
        if approvals and (approvals[0].status != "approved" or not offer_matches(offer, approvals[0])):
            raise ServiceError("The current offer needs approval before exporting a letter", status=409)
        contact = await repo.find(CandidateContact, CandidateContact.candidate_id == offer.candidate_id)
        job = await repo.get(Job, offer.job_id) if offer.job_id else None
        company = (await repo.session.execute(select(Company).where(Company.id == repo.company_id))).scalar_one()
        values = {
            **offer.details,
            "candidate": contact.name if contact and contact.name else "Candidate",
            "job": job.title if job else "Position",
            "company": company.name,
        }
        content = template.data["content"]
        for key, value in values.items():
            content = content.replace("{{" + key + "}}", str(value))
        page = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Offer letter</title><style>"
            "body{font:16px Georgia;max-width:760px;margin:50px auto;line-height:1.7}"
            "main{white-space:pre-wrap}@media print{button{display:none}}</style></head><body><main>"
            + html.escape(content)
            + "</main></body></html>"
        )
        audit(
            repo,
            principal,
            "offer.letter_exported",
            "candidate",
            offer.candidate_id,
            after={"activity_id": str(offer.id), "template_id": str(template.id)},
        )
        return Response(
            page,
            media_type="text/html",
            headers={"Content-Disposition": 'attachment; filename="offer-letter.html"', "Cache-Control": "no-store"},
        )
