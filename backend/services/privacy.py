"""Data-subject rights (DPDP Act basics): consent, export, hard deletion, retention."""

from __future__ import annotations

import base64
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, text

from backend.db import Database, TenantRepo
from backend.models import (
    Application,
    AuditLog,
    Candidate,
    CandidateContact,
    CandidateEmbedding,
    Company,
    Consent,
    Decision,
    ErasureTombstone,
    ExtractionCacheEntry,
    PoolMatch,
    QueueJob,
    Resume,
    ResumeRegistration,
    ScreeningInput,
    ScreeningResult,
    ScreeningRun,
)
from backend.queue import ClaimedJob, Queue
from backend.security import Principal
from backend.services.common import audit
from backend.storage import BlobStorage
from engine.logging_setup import log_event

logger = logging.getLogger("backend.privacy")


def _row(obj: Any, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    out = {}
    for column in obj.__table__.columns:
        if column.key in exclude:
            continue
        value = getattr(obj, column.key)
        out[column.key] = (
            value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, uuid.UUID) else value
        )
    return out


async def export_candidate(repo: TenantRepo, storage: BlobStorage, candidate_id: uuid.UUID) -> dict[str, Any]:
    """Every stored item about the candidate, including the original resume files (base64)."""
    candidate = await repo.get(Candidate, candidate_id)
    contact = await repo.find(CandidateContact, CandidateContact.candidate_id == candidate_id)
    resumes = await repo.list(Resume, Resume.candidate_id == candidate_id, order_by=[Resume.version])
    apps = await repo.list(Application, Application.candidate_id == candidate_id)
    app_ids = [a.id for a in apps]
    hashes = [r.content_hash for r in resumes]
    results = await repo.list(ScreeningResult, ScreeningResult.candidate_id == candidate_id)
    decisions = await repo.list(Decision, Decision.application_id.in_(app_ids)) if app_ids else []
    consents = await repo.list(Consent, Consent.candidate_id == candidate_id)
    registrations = await repo.list(ResumeRegistration, ResumeRegistration.resume_id.in_([r.id for r in resumes]))
    inputs = await repo.list(ScreeningInput, ScreeningInput.candidate_id == candidate_id)
    cache = await repo.list(ExtractionCacheEntry, ExtractionCacheEntry.content_hash.in_(hashes)) if hashes else []
    vectors = await repo.list(CandidateEmbedding, CandidateEmbedding.candidate_id == candidate_id)
    pool = await repo.list(PoolMatch, PoolMatch.candidate_id == candidate_id)
    object_ids = [str(candidate_id), *(str(r.id) for r in resumes), *(str(a) for a in app_ids)]
    audits = await repo.list(AuditLog, AuditLog.object_id.in_(object_ids), order_by=[AuditLog.created_at])
    files = []
    for r in resumes:
        data = await storage.read(r.blob_name) if await storage.exists(r.blob_name) else b""
        files.append({"resume_id": str(r.id), "filename": r.filename, "mime": r.mime,
                      "content_base64": base64.b64encode(data).decode()})  # fmt: skip
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "candidate": _row(candidate),
        "contact": _row(contact, exclude=("email_hash", "phone_hash")) if contact else None,
        "consents": [_row(c) for c in consents],
        "registrations": [_row(r) for r in registrations],
        "screening_inputs": [_row(i) for i in inputs],
        "resumes": [_row(r) for r in resumes],
        "resume_files": files,
        "applications": [_row(a) for a in apps],
        "screening_results": [_row(r) for r in results],
        "decisions": [_row(d) for d in decisions],
        "extractions": [_row(c) for c in cache],
        "search_vectors": [_row(v) for v in vectors],  # talent-pool vectors from redacted resume text
        "pool_matches": [_row(m) for m in pool],  # job eligibility suggestions (talent-pool consent)
        "audit_log": [_row(a) for a in audits],
    }


async def delete_candidate(
    db: Database,
    storage: BlobStorage,
    company_id: uuid.UUID,
    candidate_id: uuid.UUID,
    actor: Principal | str,
    reason: str,
    *,
    tombstone_days: int = 30,
    inactive_before: datetime | None = None,
) -> dict[str, int] | None:
    """Hard delete everything about a candidate in one transaction that holds the candidate's locks.

    Blobs, rows (contacts, resumes, registrations, applications, inputs, results, decisions and consents cascade
    from candidates), extraction cache and queued jobs are removed; erasure tombstones (keyed contact hashes) stop
    uploads registered earlier from recreating the person; a tombstone audit entry records counts only (no PII).

    Concurrency: the contact advisory locks (shared with ingestion deduplication) and the candidate row lock (shared
    with consent, screening and decisions) are held throughout, so in-flight work either finishes before the
    deletion and is deleted with it, or runs afterwards and finds nothing to write to. A blob-storage failure
    rolls the database back; the retry deletes the remaining blobs (idempotent) and then the rows.

    With ``inactive_before`` (retention), the deletion is skipped, and None returned, if the candidate has had
    activity since the sweep selected them.
    """
    async with db.tenant_session(company_id) as s:
        repo = TenantRepo(s, company_id)
        contact = await repo.find(CandidateContact, CandidateContact.candidate_id == candidate_id)
        for h in sorted(filter(None, (contact.email_hash, contact.phone_hash))) if contact else []:
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{company_id}:{h}"})
        candidate = await repo.get(Candidate, candidate_id, for_update=True)
        if inactive_before is not None and candidate.last_activity_at >= inactive_before:
            audit(repo, actor, "candidate.retention_skipped", "candidate", candidate_id,
                  after={"reason": "activity since the retention sweep"})  # fmt: skip
            log_event(logger, "candidate_retention_skipped")
            return None
        contact = await repo.find(CandidateContact, CandidateContact.candidate_id == candidate_id)
        resumes = await repo.list(Resume, Resume.candidate_id == candidate_id)
        registrations = (
            await repo.list(ResumeRegistration, ResumeRegistration.resume_id.in_([r.id for r in resumes]))
            if resumes
            else []
        )
        app_ids = [a.id for a in await repo.list(Application, Application.candidate_id == candidate_id)]
        blob_names = sorted({r.blob_name for r in resumes} | {b for g in registrations for b in g.blob_names})
        for name in blob_names:
            await storage.delete(name)

        inputs = await repo.list(ScreeningInput, ScreeningInput.candidate_id == candidate_id)
        results = await repo.list(ScreeningResult, ScreeningResult.candidate_id == candidate_id)
        for run_id in sorted({i.run_id for i in inputs} | {r.run_id for r in results}, key=str):
            removed_done = sum(r.run_id == run_id for r in results)
            removed_total = sum(i.run_id == run_id for i in inputs) or removed_done
            run = await repo.get(ScreeningRun, run_id, for_update=True)
            run.total = max(0, run.total - removed_total)
            run.done = max(0, run.done - removed_done)
            if run.done >= run.total:
                run.status = "completed"
                run.finished_at = run.finished_at or datetime.now(UTC)
        hashes = [r.content_hash for r in resumes]
        cache_deleted = 0
        if hashes:
            cache_deleted = (
                await s.execute(
                    delete(ExtractionCacheEntry).where(
                        ExtractionCacheEntry.company_id == company_id, ExtractionCacheEntry.content_hash.in_(hashes)
                    )
                )
            ).rowcount
        refs = [str(candidate_id), *(str(r.id) for r in resumes), *(str(a) for a in app_ids)]
        await s.execute(
            delete(QueueJob).where(
                QueueJob.company_id == company_id,
                QueueJob.kind != "delete_candidate",
                or_(*(QueueJob.payload[k].astext.in_(refs) for k in ("candidate_id", "resume_id", "application_id"))),
            )
        )
        await s.execute(delete(Candidate).where(Candidate.company_id == company_id, Candidate.id == candidate_id))
        expires_at = datetime.now(UTC) + timedelta(days=tombstone_days)
        tombstones = 0
        for kind, value in (("email", contact.email_hash), ("phone", contact.phone_hash)) if contact else ():
            if value:
                repo.add(ErasureTombstone(kind=kind, contact_hash=value, expires_at=expires_at))
                tombstones += 1
        counts = {
            "resumes": len(resumes),
            "blobs": len(blob_names),
            "applications": len(app_ids),
            "extraction_cache_entries": cache_deleted,
        }
        audit(repo, actor, "candidate.deleted", "candidate", candidate_id,
              after={"reason": reason, **counts, "erasure_tombstones": tombstones})  # fmt: skip
    log_event(logger, "candidate_deleted", reason=reason, **counts)
    return counts


async def process_delete_candidate(
    db: Database, storage: BlobStorage, job: ClaimedJob, *, tombstone_days: int = 30
) -> None:
    from backend.db import NotFound

    inactive_before = job.payload.get("inactive_before")
    try:
        await delete_candidate(
            db,
            storage,
            job.company_id,
            uuid.UUID(job.payload["candidate_id"]),
            job.payload.get("actor", "system:retention"),
            job.payload.get("reason", "retention"),
            tombstone_days=tombstone_days,
            inactive_before=datetime.fromisoformat(inactive_before) if inactive_before else None,
        )
    except NotFound:
        return  # already deleted (idempotent)


async def process_retention_sweep(db: Database, queue: Queue, job: ClaimedJob) -> int:
    """Queue deletion of candidates inactive for longer than the company's retention_days, and purge expired
    erasure tombstones. Each deletion re-checks inactivity under the candidate lock when it runs."""
    now = datetime.now(UTC)
    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        company = (await s.execute(select(Company).where(Company.id == job.company_id))).scalar_one()
        cutoff = now - timedelta(days=company.retention_days)
        expired = await repo.list(Candidate, Candidate.last_activity_at < cutoff)
        for c in expired:
            await queue.enqueue(
                s,
                company_id=job.company_id,
                kind="delete_candidate",
                payload={
                    "candidate_id": str(c.id),
                    "actor": "system:retention",
                    "reason": "retention",
                    "inactive_before": cutoff.isoformat(),
                },
                # Per sweep: a retried sweep is idempotent, and a candidate skipped as active (or whose earlier
                # deletion job finished) can be selected again by any later sweep.
                key=f"delete_candidate:{job.company_id}:{c.id}:{job.id}",
            )
        await s.execute(
            delete(ErasureTombstone).where(
                ErasureTombstone.company_id == job.company_id, ErasureTombstone.expires_at <= now
            )
        )
    return len(expired)


async def retention_status(repo: TenantRepo, retention_days: int) -> dict[str, Any]:
    """Counts for the retention settings panel."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    past = await repo.session.execute(
        select(func.count())
        .select_from(Candidate)
        .where(Candidate.company_id == repo.company_id, Candidate.last_activity_at < cutoff)
    )
    total = await repo.session.execute(
        select(func.count()).select_from(Candidate).where(Candidate.company_id == repo.company_id)
    )
    return {"candidates_total": total.scalar_one(), "candidates_past_retention": past.scalar_one()}
