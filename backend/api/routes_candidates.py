"""Screening runs, ranked candidates, candidate profiles, compare, consents, decisions, export and deletion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update

from backend.api.deps import Services, require, services
from backend.api.schemas import (
    CandidatesPage,
    CompareIn,
    ConsentIn,
    DecisionIn,
    RankedCandidate,
    RunInputOut,
    RunOut,
    ScreeningRunIn,
)
from backend.contacts import contact_hash
from backend.db import NotFound, TenantRepo
from backend.models import (
    Application,
    Candidate,
    CandidateContact,
    Consent,
    Decision,
    ExtractionCacheEntry,
    Job,
    Resume,
    ScreeningInput,
    ScreeningResult,
    ScreeningRun,
    User,
)
from backend.security import Principal
from backend.services.common import ServiceError, audit
from backend.services.privacy import delete_candidate, export_candidate
from backend.services.screening import create_run, ranked_results, retry_run
from backend.services.screening_control import stop_run
from backend.services.screening_sources import source_summary

router = APIRouter(prefix="/v1", tags=["screening"])
STAGE_FOR = {"shortlist": "shortlisted", "reject": "rejected", "review": "review"}


@router.post("/jobs/{job_id}/screening-runs", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("screening:run"))],
    svc: Annotated[Services, Depends(services)],
    body: ScreeningRunIn | None = None,
) -> ScreeningRun:
    async with svc.tenant(principal) as repo:
        return await create_run(repo, principal, svc.queue, job_id, source=body.source if body else None)


@router.get("/jobs/{job_id}/screening-sources")
async def screening_sources(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    async with svc.tenant(principal) as repo:
        return await source_summary(repo, job_id)


@router.get("/jobs/{job_id}/screening-runs", response_model=list[RunOut])
async def list_runs(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("jobs:read"))],
    svc: Annotated[Services, Depends(services)],
) -> list[ScreeningRun]:
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id)
        return await repo.list(ScreeningRun, ScreeningRun.job_id == job_id, order_by=[ScreeningRun.created_at.desc()])


@router.get("/screening-runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("jobs:read"))],
    svc: Annotated[Services, Depends(services)],
) -> ScreeningRun:
    async with svc.tenant(principal) as repo:
        return await repo.get(ScreeningRun, run_id)


@router.get("/screening-runs/{run_id}/inputs", response_model=list[RunInputOut])
async def run_inputs(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("jobs:read"))],
    svc: Annotated[Services, Depends(services)],
) -> list[ScreeningInput]:
    async with svc.tenant(principal) as repo:
        await repo.get(ScreeningRun, run_id)
        return await repo.list(
            ScreeningInput, ScreeningInput.run_id == run_id, order_by=[ScreeningInput.application_id]
        )


@router.post("/screening-runs/{run_id}/retry", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
async def retry_screening(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("screening:run"))],
    svc: Annotated[Services, Depends(services)],
) -> ScreeningRun:
    async with svc.tenant(principal) as repo:
        return await retry_run(repo, principal, svc.queue, run_id)


@router.post("/screening-runs/{run_id}/stop", response_model=RunOut)
async def stop_screening(
    run_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("screening:run"))],
    svc: Annotated[Services, Depends(services)],
) -> ScreeningRun:
    async with svc.tenant(principal) as repo:
        return await stop_run(repo, principal, run_id)


@router.get("/jobs/{job_id}/candidates", response_model=CandidatesPage)
async def ranked_candidates(
    job_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
    run_id: uuid.UUID | None = None,
    eligible: bool | None = None,
    stage: str | None = None,
    score_min: Annotated[float | None, Query(ge=0, le=100)] = None,
    score_max: Annotated[float | None, Query(ge=0, le=100)] = None,
    limit: Annotated[int, Query(ge=1)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> CandidatesPage:
    """Ranked results of a run (default: the job's latest run). Eligible candidates first, by score."""
    limit = min(limit, svc.settings.max_page_size)
    async with svc.tenant(principal) as repo:
        await repo.get(Job, job_id)
        if run_id is not None:
            run = await repo.get(ScreeningRun, run_id)
            if run.job_id != job_id:
                raise NotFound("run not found for this job")
        else:
            runs = await repo.list(
                ScreeningRun, ScreeningRun.job_id == job_id, order_by=[ScreeningRun.created_at.desc()], limit=1
            )
            if not runs:
                raise ServiceError("This job has no screening runs yet", status=404)
            run = runs[0]
        items, total = await ranked_results(
            repo,
            run.id,
            eligible=eligible,
            stage=stage,
            score_min=score_min,
            score_max=score_max,
            limit=limit,
            offset=offset,
        )
        return CandidatesPage(
            run_id=run.id,
            run_status=run.status,
            total=total,
            limit=limit,
            offset=offset,
            items=[RankedCandidate(**i) for i in items],
        )


@router.get("/candidates")
async def search_candidates(
    principal: Annotated[Principal, Depends(require("contacts:read"))],
    svc: Annotated[Services, Depends(services)],
    email: str | None = None,
    phone: str | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """Find candidates in your company by exact email/phone (hash match) or name substring."""
    if not (email or phone or name):
        raise ServiceError("Provide email, phone or name", status=422)
    key = svc.settings.pii_key()
    async with svc.tenant(principal) as repo:
        where = []
        if email:
            where.append(CandidateContact.email_hash == contact_hash("email", email, key))
        if phone:
            where.append(CandidateContact.phone_hash == contact_hash("phone", phone, key))
        if name:
            where.append(CandidateContact.name.ilike(f"%{name}%"))
        contacts = await repo.list(CandidateContact, *where, limit=50)
        return [{"candidate_id": str(c.candidate_id), "name": c.name, "email": c.email} for c in contacts]


async def _latest_results(repo: TenantRepo, candidate_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await repo.session.execute(
            select(ScreeningResult, ScreeningRun.job_id, ScreeningRun.requirements_version, ScreeningRun.created_at)
            .join(ScreeningRun, ScreeningRun.id == ScreeningResult.run_id)
            .where(ScreeningResult.company_id == repo.company_id, ScreeningResult.candidate_id == candidate_id)
            .order_by(ScreeningRun.created_at.desc())
        )
    ).all()
    out = []
    for result, job_id, req_version, created_at in rows:
        out.append(
            {
                "run_id": str(result.run_id),
                "job_id": str(job_id),
                "requirements_version": req_version,
                "run_created_at": created_at.isoformat(),
                "status": result.status,
                "score": result.score,
                "eligible": result.eligible,
                "knockout": result.knockout,
                "features": result.features,  # each with verified evidence quotes
                "explanation": result.explanation,
                "explanation_source": result.explanation_source,
            }
        )
    return out


@router.get("/candidates/{candidate_id}")
async def candidate_profile(
    candidate_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    """Profile, evidence and screening history. Contact details only for roles with contacts:read."""
    async with svc.tenant(principal) as repo:
        candidate = await repo.get(Candidate, candidate_id)
        resumes = await repo.list(Resume, Resume.candidate_id == candidate_id, order_by=[Resume.version])
        apps = await repo.list(Application, Application.candidate_id == candidate_id)
        consents = await repo.list(Consent, Consent.candidate_id == candidate_id)
        decisions = await _decision_history(repo, [a.id for a in apps])
        show_contacts = principal.can("contacts:read")
        profile: dict[str, Any] = {
            "candidate_id": str(candidate.id),
            "created_at": candidate.created_at.isoformat(),
            "resumes": [
                {
                    "resume_id": str(r.id),
                    "version": r.version,
                    # Filenames often contain the candidate's name: shown only to roles with contact access.
                    "filename": r.filename if show_contacts else None,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                }
                for r in resumes
            ],
            "applications": [
                {
                    "application_id": str(a.id),
                    "job_id": str(a.job_id),
                    "stage": a.stage,
                    "decisions": decisions[a.id],
                }
                for a in apps
            ],
            "consents": [_consent_out(c) for c in consents],
            "screening": await _latest_results(repo, candidate_id),
        }
        if show_contacts:
            contact = await repo.find(CandidateContact, CandidateContact.candidate_id == candidate_id)
            profile["contact"] = (
                {"name": contact.name, "email": contact.email, "phone": contact.phone} if contact else None
            )
        return profile


@router.post("/candidates/compare")
async def compare(
    body: CompareIn,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    """Side by side from ONE screening run (``run_id``, default: the job's latest run).

    Every candidate gets a ``status``: ``scored``, ``needs_reprocess`` (with ``reprocess_reason``), ``pending``
    (in the run, no result yet), ``blocked`` (excluded when the run was created, with ``reason``) or
    ``not_in_run``. Only ``scored`` rows carry scores; ``run_complete`` is false while results are pending.
    """
    async with svc.tenant(principal) as repo:
        await repo.get(Job, body.job_id)
        for cid in body.candidate_ids:
            await repo.get(Candidate, cid)
        if body.run_id is not None:
            run = await repo.get(ScreeningRun, body.run_id)
            if run.job_id != body.job_id:
                raise NotFound("run not found for this job")
        else:
            runs = await repo.list(
                ScreeningRun, ScreeningRun.job_id == body.job_id, order_by=[ScreeningRun.created_at.desc()], limit=1
            )
            if not runs:
                raise ServiceError("This job has no screening runs yet", status=404)
            run = runs[0]
        results = {
            r.candidate_id: r
            for r in await repo.list(
                ScreeningResult, ScreeningResult.run_id == run.id, ScreeningResult.candidate_id.in_(body.candidate_ids)
            )
        }
        in_run = {
            i.candidate_id
            for i in await repo.list(
                ScreeningInput, ScreeningInput.run_id == run.id, ScreeningInput.candidate_id.in_(body.candidate_ids)
            )
        }
        blocked = {b.get("candidate_id"): b.get("reason") for b in run.blocked or []}
        rows = []
        for cid in body.candidate_ids:
            r = results.get(cid)
            scored = r is not None and r.status == "scored"
            if r is not None:
                status_ = r.status
            elif cid in in_run:
                status_ = "pending"
            elif str(cid) in blocked:
                status_ = "blocked"
            else:
                status_ = "not_in_run"
            rows.append(
                {
                    "candidate_id": str(cid),
                    "status": status_,
                    "screened": r is not None,
                    "reason": blocked.get(str(cid)) if status_ == "blocked" else None,
                    "reprocess_reason": r.reprocess_reason if r is not None else None,
                    "score": r.score if scored else None,
                    "eligible": r.eligible if scored else None,
                    "features": {f["name"]: (f["value"] if f["applicable"] else None) for f in (r.features or [])}
                    if scored
                    else {},
                    "strengths": [x["text"] for x in (r.explanation or {}).get("strengths", [])] if scored else [],
                    "gaps": [x["text"] for x in (r.explanation or {}).get("gaps", [])] if scored else [],
                }
            )
        return {
            "job_id": str(body.job_id),
            "run_id": str(run.id),
            "run_status": run.status,
            "run_complete": run.status == "completed",
            "done": run.done,
            "total": run.total,
            "candidates": rows,
        }


@router.get("/candidates/{candidate_id}/consents")
async def list_consents(
    candidate_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    """Every consent grant (active and withdrawn), newest first, and whether screening is currently allowed."""
    async with svc.tenant(principal) as repo:
        await repo.get(Candidate, candidate_id)
        consents = await repo.list(Consent, Consent.candidate_id == candidate_id, order_by=[Consent.granted_at.desc()])
        return {
            "candidate_id": str(candidate_id),
            "has_active_consent": any(c.revoked_at is None for c in consents),
            "consents": [_consent_out(c) for c in consents],
        }


def _consent_out(c: Consent) -> dict[str, Any]:
    return {
        "consent_id": str(c.id),
        "source": c.source,
        "purpose": c.purpose,
        "scope": c.scope,
        "granted_at": c.granted_at.isoformat(),
        "revoked_at": c.revoked_at.isoformat() if c.revoked_at else None,
        "recorded_by": str(c.recorded_by) if c.recorded_by else None,
        "active": c.revoked_at is None,
    }


async def _decision_history(repo: TenantRepo, application_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    """Decisions per application, newest first, with the reviewer's email and role (if the user still exists)."""
    if not application_ids:
        return {}
    rows = (
        await repo.session.execute(
            select(Decision, User.email, User.role)
            .outerjoin(User, (User.id == Decision.reviewer_id) & (User.company_id == Decision.company_id))
            .where(Decision.company_id == repo.company_id, Decision.application_id.in_(application_ids))
            .order_by(Decision.created_at.desc(), Decision.id)
        )
    ).all()
    out: dict[uuid.UUID, list[dict]] = {a: [] for a in application_ids}
    for d, email, role in rows:
        out[d.application_id].append(
            {
                "decision_id": str(d.id),
                "decision": d.decision,
                "previous_stage": d.previous_stage,
                "new_stage": d.new_stage,
                "reason": d.reason,
                "decided_at": d.created_at.isoformat(),
                "reviewer": {"user_id": str(d.reviewer_id), "email": email, "role": role} if d.reviewer_id else None,
            }
        )
    return out


@router.get("/applications/{application_id}/decisions")
async def decision_history(
    application_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:read"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    async with svc.tenant(principal) as repo:
        app = await repo.get(Application, application_id)
        history = (await _decision_history(repo, [app.id]))[app.id]
        return {
            "application_id": str(app.id),
            "candidate_id": str(app.candidate_id),
            "job_id": str(app.job_id),
            "stage": app.stage,
            "decisions": history,
        }


@router.post("/candidates/{candidate_id}/consents", status_code=status.HTTP_201_CREATED)
async def record_consent(
    candidate_id: uuid.UUID,
    body: ConsentIn,
    principal: Annotated[Principal, Depends(require("consents:write"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    async with svc.tenant(principal) as repo:
        await repo.get(Candidate, candidate_id, for_update=True)
        consent = repo.add(
            Consent(
                candidate_id=candidate_id,
                source=body.source,
                purpose=body.purpose,
                scope=body.scope,
                granted_at=body.granted_at or datetime.now(UTC),
                recorded_by=principal.user_id,
            )
        )
        await repo.flush()
        audit(
            repo,
            principal,
            "consent.recorded",
            "candidate",
            candidate_id,
            after={"source": body.source, "purpose": body.purpose, "scope": body.scope},
        )
        return {"consent_id": str(consent.id), "candidate_id": str(candidate_id)}


@router.post("/candidates/{candidate_id}/consents/revoke")
async def revoke_consent(
    candidate_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("consents:write"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    async with svc.tenant(principal) as repo:
        await repo.get(Candidate, candidate_id, for_update=True)
        revoked = await repo.session.execute(
            update(Consent)
            .where(
                Consent.company_id == repo.company_id,
                Consent.candidate_id == candidate_id,
                Consent.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        resumes = await repo.list(Resume, Resume.candidate_id == candidate_id)
        await repo.session.execute(
            update(ScreeningInput)
            .where(ScreeningInput.company_id == repo.company_id, ScreeningInput.candidate_id == candidate_id)
            .values(extraction=None)
        )
        await repo.session.execute(
            sql_delete(ExtractionCacheEntry).where(
                ExtractionCacheEntry.company_id == repo.company_id,
                ExtractionCacheEntry.content_hash.in_([r.content_hash for r in resumes]),
            )
        )
        audit(repo, principal, "consent.revoked", "candidate", candidate_id, after={"revoked": revoked.rowcount})
        return {"candidate_id": str(candidate_id), "revoked": revoked.rowcount}


@router.post("/applications/{application_id}/decision", status_code=status.HTTP_201_CREATED)
async def decide(
    application_id: uuid.UUID,
    body: DecisionIn,
    principal: Annotated[Principal, Depends(require("decisions:write"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    async with svc.tenant(principal) as repo:
        app = await repo.get(Application, application_id, for_update=True)
        before = {"stage": app.stage}
        decision = repo.add(
            Decision(
                application_id=app.id,
                decision=body.decision,
                reviewer_id=principal.user_id,
                reason=body.reason,
                previous_stage=app.stage,
                new_stage=STAGE_FOR[body.decision],
            )
        )
        app.stage = STAGE_FOR[body.decision]
        await repo.flush()
        await repo.session.refresh(decision, ["created_at"])
        audit(
            repo,
            principal,
            f"decision.{body.decision}",
            "application",
            app.id,
            before=before,
            after={"stage": app.stage, "decision_id": str(decision.id), "reason": body.reason},
        )
        return {
            "decision_id": str(decision.id),
            "application_id": str(app.id),
            "stage": app.stage,
            "decision": decision.decision,
            "previous_stage": decision.previous_stage,
            "reason": decision.reason,
            "decided_at": decision.created_at.isoformat(),
            "reviewer": {"user_id": str(principal.user_id), "role": principal.role},
        }


@router.get("/candidates/{candidate_id}/export")
async def export(
    candidate_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:export"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    async with svc.tenant(principal) as repo:
        data = await export_candidate(repo, svc.storage, candidate_id)
        audit(repo, principal, "candidate.exported", "candidate", candidate_id)
        return data


@router.delete("/candidates/{candidate_id}")
async def delete(
    candidate_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("candidates:delete"))],
    svc: Annotated[Services, Depends(services)],
) -> dict[str, Any]:
    counts = await delete_candidate(
        svc.db,
        svc.storage,
        principal.company_id,
        candidate_id,
        principal,
        "erasure_request",
        tombstone_days=svc.settings.erasure_tombstone_days,
    )
    return {"deleted": True, "candidate_id": str(candidate_id), **(counts or {})}
