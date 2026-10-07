"""Persistent recruiting workflow. Hiring actions are explicitly recorded by people."""

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import String, cast, func, or_, select, text

from backend.api.deps import Services, require, services
from backend.db import TenantRepo
from backend.models import (
    STAGES,
    Application,
    Candidate,
    CandidateContact,
    Decision,
    HiringPlan,
    HRRecord,
    Job,
    RecruitingActivity,
    Resume,
    User,
)
from backend.security import Principal
from backend.services.common import ServiceError, audit
from backend.services.hr_catalog import offer_matches

router = APIRouter(prefix="/v1", tags=["recruiting"])
Stage = Literal["applied", "screened", "review", "shortlisted", "interview", "offer", "hired", "rejected", "withdrawn"]
Kind = Literal["task", "interview", "feedback", "note", "offer"]


class StageIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    stage: Stage
    reason: str = Field(min_length=2, max_length=2000)
    expected_stage: Stage | None = None


class ActivityIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Kind
    title: str = Field(min_length=2, max_length=300)
    notes: str = Field(default="", max_length=10000)
    candidate_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    assigned_to: uuid.UUID | None = None
    due_at: datetime | None = None
    end_at: datetime | None = None
    location: str = Field(default="", max_length=1000)
    technical: int | None = Field(default=None, ge=1, le=5)
    communication: int | None = Field(default=None, ge=1, le=5)
    experience: int | None = Field(default=None, ge=1, le=5)
    recommendation: Literal["hire", "no_hire", "unsure"] | None = None
    amount: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    currency: str = Field(default="INR", pattern="^[A-Z]{3}$")
    pay_period: Literal["year", "month", "hour"] = "year"
    start_date: date | None = None
    round_id: str | None = Field(default=None, max_length=40)
    plan_version: int | None = Field(default=None, ge=1)
    ratings: dict[str, Annotated[int, Field(strict=True, ge=1, le=5)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_activity(self):
        for value in (self.due_at, self.end_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Dates must include a timezone")
        if self.kind != "task" and not (self.candidate_id or self.application_id):
            raise ValueError("Choose a candidate for this record")
        if self.kind == "interview" and (not self.due_at or not self.end_at or self.end_at <= self.due_at):
            raise ValueError("Interviews need a start and a later end time")
        if self.kind == "feedback" and (
            self.technical is None
            or self.communication is None
            or self.experience is None
            or self.recommendation is None
            or len(self.notes) < 5
        ):
            raise ValueError("Feedback needs three ratings, a recommendation and supporting notes")
        if self.kind == "offer" and (self.amount is None or self.start_date is None):
            raise ValueError("Offers need compensation and an intended start date")
        return self


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["open", "completed", "cancelled", "accepted", "declined"]
    expected_updated_at: datetime | None = None


class ActivityEditIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_updated_at: datetime
    title: str = Field(min_length=2, max_length=300)
    notes: str = Field(max_length=10000)
    assigned_to: uuid.UUID | None = None
    due_at: datetime | None = None
    end_at: datetime | None = None
    location: str = Field(default="", max_length=1000)
    amount: Decimal | None = Field(default=None, ge=0, le=1_000_000_000)
    currency: str = Field(default="INR", pattern="^[A-Z]{3}$")
    pay_period: Literal["year", "month", "hour"] = "year"
    start_date: date | None = None

    @model_validator(mode="after")
    def aware_dates(self):
        for value in (self.expected_updated_at, self.due_at, self.end_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Dates must include a timezone")
        return self


async def ensure_interview_available(repo, candidate_id, owner_id, start, end, exclude_id=None):
    if owner_id:
        await repo.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"interview:{repo.company_id}:{owner_id}"},
        )
    participants = [RecruitingActivity.candidate_id == candidate_id]
    if owner_id:
        participants.append(RecruitingActivity.assigned_to == owner_id)
    filters = [
        RecruitingActivity.kind == "interview",
        RecruitingActivity.status == "open",
        RecruitingActivity.due_at < end,
        RecruitingActivity.end_at > start,
        or_(*participants),
    ]
    if exclude_id:
        filters.append(RecruitingActivity.id != exclude_id)
    if await repo.find(RecruitingActivity, *filters):
        raise ServiceError(
            "This candidate or interviewer already has an interview in that time slot. Choose another time.",
            status=409,
        )


def check_version(row, expected):
    if expected is not None and row.updated_at != expected:
        raise ServiceError("This record changed since you opened it. Refresh the workspace and try again.", status=409)


def activity_out(row: RecruitingActivity) -> dict:
    return {
        name: getattr(row, name)
        for name in (
            "id",
            "candidate_id",
            "job_id",
            "application_id",
            "created_by",
            "assigned_to",
            "kind",
            "title",
            "notes",
            "status",
            "due_at",
            "end_at",
            "details",
            "created_at",
            "updated_at",
        )
    }


async def create_activity(
    repo: TenantRepo, principal: Principal, body: ActivityIn, *, public_actor: str | None = None
) -> RecruitingActivity:
    candidate_id, job_id = body.candidate_id, body.job_id
    if body.application_id:
        application = await repo.get(Application, body.application_id)
        if (candidate_id and candidate_id != application.candidate_id) or (job_id and job_id != application.job_id):
            raise ServiceError("The selected application does not match the candidate and job", status=422)
        candidate_id, job_id = application.candidate_id, application.job_id
    if job_id:
        await repo.get(Job, job_id)
    round_details = {}
    if body.kind in {"interview", "feedback"} and job_id:
        plans = await repo.list(HiringPlan, HiringPlan.job_id == job_id, order_by=[HiringPlan.version.desc()], limit=1)
        if plans and not body.round_id:
            raise ServiceError("Choose an interview round for this job", status=422)
        if body.round_id:
            plan = await repo.find(HiringPlan, HiringPlan.job_id == job_id, HiringPlan.version == body.plan_version)
            selected_round = next((r for r in plan.rounds if r["id"] == body.round_id), None) if plan else None
            if not selected_round:
                raise ServiceError("Choose a round from the selected job plan", status=422)
            if body.kind == "feedback" and (
                set(body.ratings) != set(selected_round["criteria"])
                or any(isinstance(v, bool) or not 1 <= v <= 5 for v in body.ratings.values())
            ):
                raise ServiceError("Rate every scorecard criterion from 1 to 5", status=422)
            round_details = {
                "round_id": body.round_id,
                "round_name": selected_round["name"],
                "plan_version": plan.version,
                "criteria": selected_round["criteria"],
                "ratings": body.ratings,
            }
    elif body.round_id or body.plan_version or body.ratings:
        raise ServiceError("Choose a job for a job-specific interview round", status=422)
    if body.assigned_to:
        owner = await repo.get(User, body.assigned_to)
        if not owner.is_active:
            raise ServiceError("Choose an active team member", status=422)
    if candidate_id:
        candidate = await repo.get(Candidate, candidate_id, for_update=True)
        candidate.last_activity_at = datetime.now(UTC)
    if body.kind == "interview":
        await ensure_interview_available(
            repo, candidate_id, body.assigned_to or principal.user_id, body.due_at, body.end_at
        )
    fields = {
        "interview": ["location"],
        "feedback": ["technical", "communication", "experience", "recommendation"],
        "offer": ["amount", "currency", "pay_period", "start_date"],
    }.get(body.kind, [])
    dumped = body.model_dump(mode="json")
    row = repo.add(
        RecruitingActivity(
            id=uuid.uuid4(),
            candidate_id=candidate_id,
            job_id=job_id,
            application_id=body.application_id,
            created_by=None if public_actor else principal.user_id,
            assigned_to=body.assigned_to or principal.user_id,
            kind=body.kind,
            title=body.title,
            notes=body.notes,
            status="completed" if body.kind in ("feedback", "note") else "open",
            due_at=body.due_at,
            end_at=body.end_at,
            details={**{name: dumped[name] for name in fields}, **round_details},
        )
    )
    await repo.flush()
    audit(
        repo,
        public_actor or principal,
        "recruiting.created",
        "candidate" if candidate_id else "recruiting_activity",
        candidate_id or row.id,
        after={"activity_id": str(row.id), "kind": row.kind},
    )
    return row


@router.get("/recruiting/team")
async def recruiting_team(
    principal: Annotated[Principal, Depends(require("candidates:read"))], svc: Annotated[Services, Depends(services)]
) -> list[dict]:
    async with svc.tenant(principal) as repo:
        return [
            {
                "id": str(u.id),
                "role": u.role,
                "label": u.email if principal.can("contacts:read") else f"{u.role} {str(u.id)[:8]}",
            }
            for u in await repo.list(User, User.is_active.is_(True), order_by=[User.email])
        ]


@router.get("/jobs/{job_id}/pipeline")
async def pipeline(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
    stage: Stage | None = None,
    offset: int = 0,
    q: str = Query(default="", max_length=200),
) -> dict:
    if offset < 0:
        raise ServiceError("Offset must be non-negative", status=422)
    async with svc.tenant(principal) as repo:
        job = await repo.get(Job, job_id)
        base = repo.select(Application, Application.job_id == job_id)
        counts = dict(
            (
                await repo.session.execute(
                    select(Application.stage, func.count())
                    .where(Application.company_id == repo.company_id, Application.job_id == job_id)
                    .group_by(Application.stage)
                )
            ).all()
        )
        if stage:
            base = base.where(Application.stage == stage)
        if term := q.strip().lower():
            matches = [cast(Application.candidate_id, String).contains(term, autoescape=True)]
            if principal.can("contacts:read"):
                matches.extend(
                    [
                        repo.select(CandidateContact, CandidateContact.candidate_id == Application.candidate_id)
                        .where(func.lower(CandidateContact.name).contains(term, autoescape=True))
                        .exists(),
                        repo.select(Resume, Resume.candidate_id == Application.candidate_id)
                        .where(func.lower(Resume.filename).contains(term, autoescape=True))
                        .exists(),
                    ]
                )
            base = base.where(or_(*matches))
        total = (await repo.session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        apps = (
            (
                await repo.session.execute(
                    base.order_by(Application.created_at.desc(), Application.id).limit(50).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        names = {}
        if principal.can("contacts:read") and apps:
            names = {
                c.candidate_id: c.name
                for c in await repo.list(
                    CandidateContact, CandidateContact.candidate_id.in_([a.candidate_id for a in apps])
                )
            }
        return {
            "job": {"id": str(job.id), "title": job.title},
            "stages": STAGES,
            "counts": counts,
            "total": total,
            "items": [
                {
                    "id": str(a.id),
                    "candidate_id": str(a.candidate_id),
                    "name": names.get(a.candidate_id),
                    "stage": a.stage,
                    "created_at": a.created_at,
                }
                for a in apps
            ],
        }


@router.patch("/applications/{application_id}/stage")
async def move_stage(
    application_id: uuid.UUID,
    body: StageIn,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> dict:
    async with svc.tenant(principal) as repo:
        original = await repo.get(Application, application_id)
        candidate = await repo.get(Candidate, original.candidate_id, for_update=True)
        app = await repo.get(Application, application_id, for_update=True)
        await repo.session.refresh(app)
        if body.expected_stage and app.stage != body.expected_stage:
            raise ServiceError(
                "This stage changed since you opened it. Refresh the pipeline and try again.", status=409
            )
        previous = app.stage
        if previous != body.stage:
            app.stage = body.stage
            candidate.last_activity_at = datetime.now(UTC)
            repo.add(
                Decision(
                    application_id=app.id,
                    decision="move",
                    previous_stage=previous,
                    new_stage=app.stage,
                    reviewer_id=principal.user_id,
                    reason=body.reason,
                )
            )
            audit(
                repo,
                principal,
                "application.stage_changed",
                "application",
                app.id,
                before={"stage": previous},
                after={"stage": app.stage},
            )
        return {"stage": app.stage, "previous_stage": previous}


@router.get("/recruiting/activities")
async def activities(
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
    kind: Kind | None = None,
    job_id: uuid.UUID | None = None,
    candidate_id: uuid.UUID | None = None,
    offset: int = 0,
    status: Literal["open", "completed", "cancelled", "accepted", "declined"] | None = None,
    assigned_to: uuid.UUID | None = None,
    overdue: bool = False,
    q: str = Query(default="", max_length=200),
) -> dict:
    if offset < 0:
        raise ServiceError("Offset must be non-negative", status=422)
    async with svc.tenant(principal) as repo:
        filters = []
        if kind:
            filters.append(RecruitingActivity.kind == kind)
        if job_id:
            filters.append(RecruitingActivity.job_id == job_id)
        if candidate_id:
            filters.append(RecruitingActivity.candidate_id == candidate_id)
        if status:
            filters.append(RecruitingActivity.status == status)
        if assigned_to:
            filters.append(RecruitingActivity.assigned_to == assigned_to)
        if overdue:
            filters.extend(
                [
                    RecruitingActivity.kind == "task",
                    RecruitingActivity.status == "open",
                    RecruitingActivity.due_at < datetime.now(UTC),
                ]
            )
        if term := q.strip().lower():
            matches = [func.lower(RecruitingActivity.title).contains(term, autoescape=True)]
            matches.append(
                repo.select(Job, Job.id == RecruitingActivity.job_id)
                .where(func.lower(Job.title).contains(term, autoescape=True))
                .exists()
            )
            if principal.can("contacts:read"):
                matches.append(
                    repo.select(CandidateContact, CandidateContact.candidate_id == RecruitingActivity.candidate_id)
                    .where(func.lower(CandidateContact.name).contains(term, autoescape=True))
                    .exists()
                )
            filters.append(or_(*matches))
        if not principal.can("contacts:read"):
            filters.append(RecruitingActivity.kind != "offer")
        base = repo.select(RecruitingActivity, *filters)
        total = (await repo.session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
        rows = (
            (
                await repo.session.execute(
                    base.order_by(RecruitingActivity.created_at.desc(), RecruitingActivity.id).limit(50).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        names = {}
        if principal.can("contacts:read") and rows:
            names = {
                c.candidate_id: c.name
                for c in await repo.list(
                    CandidateContact,
                    CandidateContact.candidate_id.in_([r.candidate_id for r in rows if r.candidate_id]),
                )
            }
        titles = (
            {j.id: j.title for j in await repo.list(Job, Job.id.in_([r.job_id for r in rows if r.job_id]))}
            if rows
            else {}
        )
        return {
            "total": total,
            "items": [
                {
                    **activity_out(row),
                    "candidate_name": names.get(row.candidate_id),
                    "job_title": titles.get(row.job_id),
                }
                for row in rows
            ],
        }


@router.post("/recruiting/activities", status_code=201)
async def add_activity(
    body: ActivityIn,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> dict:
    async with svc.tenant(principal) as repo:
        return activity_out(await create_activity(repo, principal, body))


@router.post("/recruiting/feedback", status_code=201)
async def add_feedback(
    body: ActivityIn,
    principal: Annotated[Principal, Depends(require("decisions:write"))],
    svc: Annotated[Services, Depends(services)],
) -> dict:
    if body.kind != "feedback":
        raise ServiceError("This endpoint accepts interview feedback only", status=422)
    async with svc.tenant(principal) as repo:
        return activity_out(await create_activity(repo, principal, body))


@router.patch("/recruiting/activities/{activity_id}")
async def change_status(
    activity_id: uuid.UUID,
    body: StatusIn,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> dict:
    async with svc.tenant(principal) as repo:
        row = await repo.get(RecruitingActivity, activity_id)
        if row.candidate_id:
            candidate = await repo.get(Candidate, row.candidate_id, for_update=True)
            candidate.last_activity_at = datetime.now(UTC)
        row = await repo.get(RecruitingActivity, activity_id, for_update=True)
        await repo.session.refresh(row)
        check_version(row, body.expected_updated_at)
        allowed = {
            "offer": {"open", "accepted", "declined", "cancelled"},
            "task": {"open", "completed", "cancelled"},
            "interview": {"open", "completed", "cancelled"},
        }
        if body.status not in allowed.get(row.kind, set()):
            raise ServiceError("That status is not valid for this record; notes and feedback are immutable", status=422)
        if row.kind == "offer" and body.status == "accepted":
            requests = await repo.list(
                HRRecord,
                HRRecord.kind == "offer_approval",
                HRRecord.activity_id == row.id,
                order_by=[HRRecord.created_at.desc()],
                limit=1,
            )
            if requests and (requests[0].status != "approved" or not offer_matches(row, requests[0])):
                raise ServiceError("The current offer needs approval before recording acceptance", status=409)
        if row.kind == "interview" and body.status == "open":
            await ensure_interview_available(repo, row.candidate_id, row.assigned_to, row.due_at, row.end_at, row.id)
        previous = row.status
        row.status, row.updated_at = body.status, datetime.now(UTC)
        audit(
            repo,
            principal,
            "recruiting.status_changed",
            "candidate" if row.candidate_id else "recruiting_activity",
            row.candidate_id or row.id,
            before={"status": previous},
            after={"status": row.status, "activity_id": str(row.id)},
        )
        return activity_out(row)


@router.patch("/recruiting/activities/{activity_id}/details")
async def edit_activity(
    activity_id: uuid.UUID,
    body: ActivityEditIn,
    principal: Annotated[Principal, Depends(require("recruiting:manage"))],
    svc: Annotated[Services, Depends(services)],
) -> dict:
    async with svc.tenant(principal) as repo:
        row = await repo.get(RecruitingActivity, activity_id)
        if row.candidate_id:
            candidate = await repo.get(Candidate, row.candidate_id, for_update=True)
            candidate.last_activity_at = datetime.now(UTC)
        row = await repo.get(RecruitingActivity, activity_id, for_update=True)
        check_version(row, body.expected_updated_at)
        if row.kind not in {"task", "interview", "offer"}:
            raise ServiceError(
                "Feedback and notes are historical records. Add another record to supplement them.", status=422
            )
        fields = {
            "task": {"due_at"},
            "interview": {"due_at", "end_at", "location"},
            "offer": {"amount", "currency", "pay_period", "start_date"},
        }[row.kind]
        changes = body.model_dump(exclude_unset=True, exclude={"expected_updated_at"})
        if changes.keys() - (fields | {"title", "notes", "assigned_to"}):
            raise ServiceError("These fields are not valid for this record type", status=422)
        data = {
            "kind": row.kind,
            "title": row.title,
            "notes": row.notes,
            "candidate_id": row.candidate_id,
            "job_id": row.job_id,
            "application_id": row.application_id,
            "assigned_to": row.assigned_to,
            "due_at": row.due_at,
            "end_at": row.end_at,
            **{key: value for key, value in row.details.items() if key in ActivityIn.model_fields},
            **changes,
        }
        # The same validations apply to creation and edits; no identity links can be changed.
        try:
            validated = ActivityIn.model_validate(data)
        except ValidationError as exc:
            raise ServiceError(exc.errors()[0]["msg"], status=422) from exc
        if validated.assigned_to:
            owner = await repo.get(User, validated.assigned_to)
            if not owner.is_active:
                raise ServiceError("Choose an active team member", status=422)
        if row.kind == "interview" and row.status == "open":
            await ensure_interview_available(
                repo, row.candidate_id, validated.assigned_to, validated.due_at, validated.end_at, row.id
            )
        previous_version = row.updated_at
        row.title, row.notes, row.assigned_to = validated.title, validated.notes, validated.assigned_to
        row.due_at, row.end_at = validated.due_at, validated.end_at
        dumped = validated.model_dump(mode="json")
        detail_keys = fields - {"due_at", "end_at"}
        row.details = {**row.details, **{key: dumped[key] for key in detail_keys}}
        row.updated_at = datetime.now(UTC)
        audit(
            repo,
            principal,
            "recruiting.edited",
            "candidate" if row.candidate_id else "recruiting_activity",
            row.candidate_id or row.id,
            before={"updated_at": previous_version.isoformat()},
            after={"activity_id": str(row.id), "fields": sorted(changes), "updated_at": row.updated_at.isoformat()},
        )
        await repo.flush()
        return activity_out(row)


def ics_text(row: RecruitingActivity) -> str:
    def escape(value):
        return (
            value.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\r\n", "\\n")
            .replace("\n", "\\n")
            .replace("\r", "\\n")
        )

    def stamp(value):
        return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Shortlist//Recruiting//EN",
        "BEGIN:VEVENT",
        f"UID:{row.id}@shortlist",
        f"DTSTAMP:{stamp(datetime.now(UTC))}",
        f"DTSTART:{stamp(row.due_at)}",
        f"DTEND:{stamp(row.end_at)}",
        "STATUS:CANCELLED" if row.status == "cancelled" else "STATUS:CONFIRMED",
        f"SUMMARY:{escape(row.title)}",
        f"DESCRIPTION:{escape(row.notes)}",
        f"LOCATION:{escape(row.details.get('location', ''))}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    folded = []
    for line in lines:
        current = ""
        for char in line:
            if len((current + char).encode()) > 75:
                folded.append(current)
                current = " "
            current += char
        folded.append(current)
    return "\r\n".join(folded) + "\r\n"


@router.get("/recruiting/activities/{activity_id}/calendar")
async def calendar_export(
    activity_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("contacts:read"))],
    svc: Annotated[Services, Depends(services)],
) -> Response:
    async with svc.tenant(principal) as repo:
        row = await repo.get(RecruitingActivity, activity_id)
        if row.kind != "interview" or not row.due_at or not row.end_at:
            raise ServiceError("Only interviews have calendar exports", status=422)
        return Response(
            ics_text(row),
            media_type="text/calendar",
            headers={"Content-Disposition": 'attachment; filename="interview.ics"', "Cache-Control": "no-store"},
        )


@router.get("/recruiting/reports")
async def reports(
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict:
    if from_date and to_date and to_date < from_date:
        raise ServiceError("Report end date must be on or after the start date", status=422)
    cohort = []
    if from_date:
        cohort.append(Application.created_at >= datetime.combine(from_date, datetime.min.time(), UTC))
    if to_date and to_date != date.max:
        cohort.append(Application.created_at < datetime.combine(to_date + timedelta(days=1), datetime.min.time(), UTC))
    async with svc.tenant(principal) as repo:
        grouped = (
            await repo.session.execute(
                select(Application.job_id, Application.stage, func.count())
                .where(Application.company_id == repo.company_id, *cohort)
                .group_by(Application.job_id, Application.stage)
            )
        ).all()
        jobs = await repo.list(Job, order_by=[Job.created_at.desc()])
        counts, by_job = {stage: 0 for stage in STAGES}, {}
        for job_id, stage, count in grouped:
            counts[stage] += count
            by_job.setdefault(job_id, {})[stage] = count
        now = datetime.now(UTC)
        open_counts = dict(
            (
                await repo.session.execute(
                    select(RecruitingActivity.kind, func.count())
                    .where(RecruitingActivity.company_id == repo.company_id, RecruitingActivity.status == "open")
                    .group_by(RecruitingActivity.kind)
                )
            ).all()
        )
        overdue = (
            await repo.session.execute(
                select(func.count())
                .select_from(RecruitingActivity)
                .where(
                    RecruitingActivity.company_id == repo.company_id,
                    RecruitingActivity.kind == "task",
                    RecruitingActivity.status == "open",
                    RecruitingActivity.due_at < now,
                )
            )
        ).scalar_one()
        hires = (
            select(Decision.application_id, func.min(Decision.created_at).label("hired_at"))
            .where(Decision.company_id == repo.company_id, Decision.new_stage == "hired")
            .group_by(Decision.application_id)
            .subquery()
        )
        duration = (
            await repo.session.execute(
                select(func.avg(func.extract("epoch", hires.c.hired_at - Application.created_at) / 86400))
                .select_from(Application)
                .join(hires, hires.c.application_id == Application.id)
                .where(Application.company_id == repo.company_id, *cohort)
            )
        ).scalar_one_or_none()
        hires30 = (
            await repo.session.execute(
                select(func.count()).select_from(hires).where(hires.c.hired_at >= now - timedelta(days=30))
            )
        ).scalar_one()
        sources = dict(
            (
                await repo.session.execute(
                    select(Application.source_type, func.count())
                    .where(Application.company_id == repo.company_id, *cohort)
                    .group_by(Application.source_type)
                )
            ).all()
        )
        conversions = dict(
            (
                await repo.session.execute(
                    select(Decision.new_stage, func.count(func.distinct(Decision.application_id)))
                    .join(Application, Application.id == Decision.application_id)
                    .where(Decision.company_id == repo.company_id, Application.company_id == repo.company_id, *cohort)
                    .group_by(Decision.new_stage)
                )
            ).all()
        )
        month = func.to_char(func.timezone("UTC", Application.created_at), "YYYY-MM")
        reached = (
            select(Decision.application_id, Decision.new_stage)
            .where(Decision.company_id == repo.company_id)
            .distinct()
            .subquery()
        )
        monthly = (
            (
                await repo.session.execute(
                    select(
                        month.label("month"),
                        func.count(func.distinct(Application.id)).label("applications"),
                        *(
                            func.count(func.distinct(Application.id)).filter(reached.c.new_stage == stage).label(stage)
                            for stage in ("interview", "offer", "hired")
                        ),
                    )
                    .outerjoin(reached, reached.c.application_id == Application.id)
                    .where(Application.company_id == repo.company_id, *cohort)
                    .group_by(month)
                    .order_by(month.desc())
                    .limit(24)
                )
            )
            .mappings()
            .all()
        )
        return {
            "monthly_cohorts": [dict(row) for row in reversed(monthly)],
            "sources": sources,
            "reached_stages": conversions,
            "cohort": {"from_date": from_date, "to_date": to_date},
            "stages": counts,
            "applications": sum(counts.values()),
            "jobs": len(jobs),
            "open_tasks": open_counts.get("task", 0),
            "open_interviews": open_counts.get("interview", 0),
            "open_offers": open_counts.get("offer", 0) if principal.can("contacts:read") else None,
            "overdue_tasks": overdue,
            "hires_last_30_days": hires30,
            "average_days_to_hire": round(float(duration), 1) if duration is not None else None,
            "by_job": [{"id": str(job.id), "title": job.title, "stages": by_job.get(job.id, {})} for job in jobs],
        }


@router.get("/recruiting/workload")
async def workload(
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
    mine: bool = True,
):
    async with svc.tenant(principal) as repo:
        filters = [RecruitingActivity.status == "open", RecruitingActivity.kind.in_(["task", "interview"])]
        if mine:
            filters.append(RecruitingActivity.assigned_to == principal.user_id)
        rows = await repo.list(
            RecruitingActivity,
            *filters,
            order_by=[RecruitingActivity.due_at.asc().nullslast(), RecruitingActivity.id],
            limit=50,
        )
        summary = (
            await repo.session.execute(
                select(RecruitingActivity.assigned_to, RecruitingActivity.kind, func.count())
                .where(RecruitingActivity.company_id == repo.company_id, *filters)
                .group_by(RecruitingActivity.assigned_to, RecruitingActivity.kind)
            )
        ).all()
        now = datetime.now(UTC)
        return {
            "by_owner": [{"owner_id": owner, "kind": kind, "count": count} for owner, kind, count in summary],
            "items": [
                {
                    **activity_out(row),
                    "overdue": bool(row.due_at and row.due_at < now),
                    "due_soon": bool(row.due_at and now <= row.due_at <= now + timedelta(days=7)),
                }
                for row in rows
            ],
        }
