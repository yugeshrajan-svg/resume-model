"""Resume registration and the extract_resume job (parse/OCR, contacts, dedup, redaction, versioning)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text

from backend.config import PlatformSettings
from backend.contacts import Contacts, contact_hash, extract_contacts, redact
from backend.db import Database, NotFound, TenantRepo
from backend.models import (
    Application,
    Candidate,
    CandidateContact,
    Consent,
    ErasureTombstone,
    Job,
    Resume,
    ResumeRegistration,
)
from backend.queue import ClaimedJob, Queue
from backend.security import Principal
from backend.services.common import ServiceError, audit, record_usage
from backend.services.talent_pool import enqueue_embedding
from backend.storage import BlobStorage, belongs_to
from engine import __version__ as ENGINE_VERSION
from engine.config import get_settings as get_engine_settings
from engine.logging_setup import log_event
from engine.parsing import (
    SUPPORTED_EXTENSIONS,
    FileTooLarge,
    ParseError,
    UnsupportedFileType,
    detect_file_type,
    parse_bytes,
)

logger = logging.getLogger("backend.resumes")

MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
}


@dataclass(frozen=True)
class RegisterItem:
    blob_name: str
    filename: str
    job_id: uuid.UUID | None
    consent: dict[str, Any] | None  # {"source", "purpose", "granted_at"?}


def validate_upload_request(filename: str, size: int, max_bytes: int) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if f".{ext}" not in SUPPORTED_EXTENSIONS:
        raise ServiceError(f"Unsupported file type {ext!r}; expected .pdf, .docx or .txt", status=415)
    if size <= 0 or size > max_bytes:
        raise ServiceError(f"File size must be between 1 and {max_bytes} bytes", status=413)
    return MIME_TYPES[SUPPORTED_EXTENSIONS[f".{ext}"]]


async def register_resume(
    repo: TenantRepo, principal: Principal, storage: BlobStorage, queue: Queue, item: RegisterItem
) -> tuple[Resume, bool]:
    """Validate an uploaded blob (tenant prefix, size, type, magic bytes) and queue extraction.

    Idempotent by content hash within the company: re-registering the same file returns the existing resume.
    Returns (resume, created).
    """
    engine_settings = get_engine_settings()
    if not belongs_to(repo.company_id, item.blob_name):
        raise ServiceError("Unknown upload", status=404)  # other tenants' blobs look like missing ones
    if item.job_id is not None:
        await repo.get(Job, item.job_id)
    validate_upload_request(item.filename, 1, engine_settings.max_file_bytes)
    existing = await repo.find(Resume, Resume.blob_name == item.blob_name)
    if existing is None:
        alias = await repo.find(ResumeRegistration, ResumeRegistration.blob_names.contains([item.blob_name]))
        existing = await repo.get(Resume, alias.resume_id) if alias else None
    if existing is not None:
        existing = await repo.get(Resume, existing.id, for_update=True)
        await _register_association(repo, principal, existing, item)
        return existing, False
    size = await storage.size(item.blob_name)
    if size is None:
        raise ServiceError("Upload not found; PUT the file to the SAS URL first", status=404)
    if size > engine_settings.max_file_bytes:
        raise ServiceError(f"File is {size} bytes; limit is {engine_settings.max_file_bytes}", status=413)
    data = await storage.read(item.blob_name)
    try:
        file_type = detect_file_type(data, item.filename, engine_settings)  # extension + magic bytes + size
    except ParseError as exc:
        if await repo.find(Resume, Resume.blob_name == item.blob_name) is None:
            await storage.delete(item.blob_name)
        status = 415 if isinstance(exc, UnsupportedFileType) else 413 if isinstance(exc, FileTooLarge) else 400
        raise ServiceError(str(exc), status=status) from exc

    content_hash = hashlib.sha256(data).hexdigest()
    await repo.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"register:{repo.company_id}:{content_hash}"},
    )
    existing = (
        await repo.session.execute(repo.select(Resume, Resume.content_hash == content_hash).with_for_update())
    ).scalar_one_or_none()
    if existing is not None:
        await _register_association(repo, principal, existing, item)
        if existing.blob_name != item.blob_name:
            await storage.delete(item.blob_name)  # duplicate upload: keep a single copy
        return existing, False

    resume = repo.add(
        Resume(
            id=uuid.uuid4(),
            blob_uri=storage.uri(item.blob_name),
            blob_name=item.blob_name,
            filename=item.filename,
            content_hash=content_hash,
            mime=MIME_TYPES[file_type],
            size=size,
            status="registered",
        )
    )
    await repo.flush()
    await _register_association(repo, principal, resume, item)
    await queue.enqueue(
        repo.session,
        company_id=repo.company_id,
        kind="extract_resume",
        payload={"resume_id": str(resume.id), "generation": resume.processing_generation},
        key=f"extract_resume:{repo.company_id}:{content_hash}:{ENGINE_VERSION}",
    )
    audit(repo, principal, "resume.registered", "resume", resume.id, after={"size": size, "mime": resume.mime})
    return resume, True


async def _register_association(repo: TenantRepo, principal: Principal, resume: Resume, item: RegisterItem) -> None:
    registration = await repo.find(
        ResumeRegistration, ResumeRegistration.resume_id == resume.id, ResumeRegistration.job_id == item.job_id
    )
    if registration is None:
        registration = repo.add(
            ResumeRegistration(
                resume_id=resume.id,
                job_id=item.job_id,
                consent=item.consent,
                recorded_by=principal.user_id,
                blob_names=[item.blob_name],
            )
        )
    elif registration.applied_at is None and item.consent:
        registration.consent = item.consent
        registration.recorded_by = principal.user_id
    elif registration.consent is None and item.consent:
        registration.consent = item.consent
        registration.recorded_by = principal.user_id
        registration.applied_at = None
    if item.blob_name not in registration.blob_names:
        registration.blob_names = [*registration.blob_names, item.blob_name]
    await repo.flush()
    if resume.candidate_id is not None:
        await _apply_association(repo, resume, registration)


async def _apply_association(repo: TenantRepo, resume: Resume, registration: ResumeRegistration) -> None:
    candidate = await repo.get(Candidate, resume.candidate_id, for_update=True)
    if registration.applied_at is None and registration.consent:
        c = registration.consent
        repo.add(
            Consent(
                candidate_id=candidate.id,
                source=c["source"],
                purpose=c["purpose"],
                scope=c.get("scope", "job"),
                granted_at=datetime.fromisoformat(c["granted_at"]) if c.get("granted_at") else datetime.now(UTC),
                recorded_by=registration.recorded_by,
            )
        )
    if registration.job_id is not None:
        app = await repo.find(
            Application, Application.job_id == registration.job_id, Application.candidate_id == candidate.id
        )
        if app is None:
            repo.add(Application(job_id=registration.job_id, candidate_id=candidate.id, resume_id=resume.id))
        else:
            # Same row lock as the decision endpoint: a manual stage set concurrently is seen, never overwritten.
            app = await repo.get(Application, app.id, for_update=True)
            previous = await repo.get(Resume, app.resume_id)
            if (resume.version or 0) >= (previous.version or 0):
                app.resume_id = resume.id
                if (resume.version or 0) > (previous.version or 0) and app.stage == "screened":
                    app.stage = "applied"
    registration.applied_at = registration.applied_at or datetime.now(UTC)
    candidate.last_activity_at = datetime.now(UTC)


async def reprocess_resume(repo: TenantRepo, principal: Principal, queue: Queue, resume_id: uuid.UUID) -> Resume:
    resume = await repo.get(Resume, resume_id, for_update=True)
    if resume.status not in {"failed", "needs_ocr"}:
        raise ServiceError("Only failed or needs_ocr resumes can be reprocessed")
    resume.processing_generation += 1
    resume.status, resume.status_reason = "registered", None
    await queue.enqueue(
        repo.session,
        company_id=repo.company_id,
        kind="extract_resume",
        payload={"resume_id": str(resume.id), "generation": resume.processing_generation},
        key=f"reprocess_resume:{resume.id}:{resume.processing_generation}",
    )
    audit(
        repo,
        principal,
        "resume.reprocess_requested",
        "resume",
        resume.id,
        after={"generation": resume.processing_generation},
    )
    return resume


async def _find_duplicate(repo: TenantRepo, email_hash: str | None, phone_hash: str | None) -> Candidate | None:
    for column, value in ((CandidateContact.email_hash, email_hash), (CandidateContact.phone_hash, phone_hash)):
        if value:
            contact = await repo.find(CandidateContact, column == value)
            if contact is not None:
                return await repo.get(Candidate, contact.candidate_id)
    return None


async def _erased_after_upload(
    repo: TenantRepo, resume: Resume, email_hash: str | None, phone_hash: str | None
) -> bool:
    """True if a candidate with this email/phone was erased after this resume was registered."""
    hashes = [h for h in (email_hash, phone_hash) if h]
    if not hashes:
        return False
    return (
        await repo.find(
            ErasureTombstone,
            ErasureTombstone.contact_hash.in_(hashes),
            ErasureTombstone.erased_at >= resume.created_at,
            ErasureTombstone.expires_at > func.now(),
        )
    ) is not None


async def process_extract_resume(
    db: Database, storage: BlobStorage, settings: PlatformSettings, job: ClaimedJob, *, queue: Queue | None = None
) -> None:
    """Parse, redact and resolve the candidate. With ``queue``, a ready resume's talent-pool vector is queued in
    the same transaction."""
    resume_id = uuid.UUID(job.payload["resume_id"])
    generation = job.payload.get("generation", 1)
    async with db.tenant_session(job.company_id) as s:
        resume = await TenantRepo(s, job.company_id).get(Resume, resume_id)
        if resume.processing_generation != generation or resume.processed_generation >= generation:
            return  # already processed (idempotent re-run)
        blob_name, filename = resume.blob_name, resume.filename

    data = await storage.read(blob_name)
    if hashlib.sha256(data).hexdigest() != resume.content_hash:
        raise ServiceError("Stored resume content changed")
    try:
        doc = await asyncio.to_thread(parse_bytes, data, filename)
    except ParseError as exc:
        async with db.tenant_session(job.company_id) as s:
            r = await TenantRepo(s, job.company_id).get(Resume, resume_id, for_update=True)
            if r.processing_generation == generation:
                r.status, r.status_reason = "failed", str(exc)[:500]
                r.processed_generation = generation
        return  # permanent: retrying cannot help

    contacts: Contacts = extract_contacts(doc.text)
    redacted = redact(doc.text, contacts)
    key = settings.pii_key()
    email_hash = contact_hash("email", contacts.email, key) if contacts.email else None
    phone_hash = contact_hash("phone", contacts.phone, key) if contacts.phone else None

    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        # Serialize dedup per contact so two resumes of one person processed concurrently share a candidate.
        for h in sorted(filter(None, (email_hash, phone_hash))):
            await s.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{job.company_id}:{h}"}
            )
        resume = await repo.get(Resume, resume_id, for_update=True)
        if resume.processing_generation != generation or resume.processed_generation >= generation:
            return
        if resume.candidate_id is None and await _erased_after_upload(repo, resume, email_hash, phone_hash):
            # Uploaded before this person's erasure: discard instead of recreating the erased candidate.
            await storage.delete(resume.blob_name)
            await s.execute(delete(Resume).where(Resume.company_id == job.company_id, Resume.id == resume.id))
            audit(repo, "system:worker", "resume.discarded_after_erasure", "resume", resume.id)
            log_event(logger, "resume_discarded_after_erasure", resume_id=str(resume_id))
            return
        candidate = (
            await repo.get(Candidate, resume.candidate_id, for_update=True)
            if resume.candidate_id
            else await _find_duplicate(repo, email_hash, phone_hash)
        )
        deduplicated = candidate is not None
        if candidate is None:
            candidate = repo.add(Candidate(id=uuid.uuid4()))
            await repo.flush()
            repo.add(
                CandidateContact(
                    candidate_id=candidate.id,
                    name=contacts.name,
                    email=contacts.email,
                    phone=contacts.phone,
                    email_hash=email_hash,
                    phone_hash=phone_hash,
                )
            )
        else:
            candidate = await repo.get(Candidate, candidate.id, for_update=True)
            contact = await repo.find(CandidateContact, CandidateContact.candidate_id == candidate.id)
            if contact is not None:  # keep the newest non-empty details
                contact.name = contacts.name or contact.name
                contact.email = contacts.email or contact.email
                contact.phone = contacts.phone or contact.phone
                contact.email_hash = email_hash or contact.email_hash
                contact.phone_hash = phone_hash or contact.phone_hash
                contact.updated_at = datetime.now(UTC)
        latest = (
            await s.execute(
                select(func.max(Resume.version)).where(
                    Resume.company_id == job.company_id, Resume.candidate_id == candidate.id
                )
            )
        ).scalar()
        resume.candidate_id = candidate.id
        resume.version = resume.version or (latest or 0) + 1
        resume.processed_generation = generation
        resume.redacted_text = redacted
        resume.hidden_items_removed = doc.hidden_items_removed
        resume.ocr_pages = doc.ocr_pages
        resume.status = "needs_ocr" if doc.needs_ocr and not doc.ocr_applied else "ready"
        resume.status_reason = "scanned document; OCR unavailable or failed" if resume.status == "needs_ocr" else None
        candidate.last_activity_at = datetime.now(UTC)

        for registration in await repo.list(ResumeRegistration, ResumeRegistration.resume_id == resume.id):
            await _apply_association(repo, resume, registration)
        if queue is not None and resume.status == "ready":
            await enqueue_embedding(repo, queue, resume.id)
        if doc.ocr_pages:
            price = get_engine_settings().resolved_ocr_price_per_page
            record_usage(repo, "ocr", ocr_pages=doc.ocr_pages, cost_usd=round(doc.ocr_pages * price, 6))
        audit(
            repo,
            "system:worker",
            "resume.processed",
            "resume",
            resume.id,
            after={
                "candidate_id": str(candidate.id),
                "version": resume.version,
                "deduplicated": deduplicated,
                "status": resume.status,
                "hidden_items_removed": doc.hidden_items_removed,
                "ocr_pages": doc.ocr_pages,
            },
        )
    log_event(logger, "resume_processed", resume_id=str(resume_id), deduplicated=deduplicated, ocr_pages=doc.ocr_pages)


async def record_dead_extract_job(db: Database, job: ClaimedJob, error: str) -> None:
    """Expose exhausted ingestion jobs in the resume status instead of leaving them registered forever."""
    async with db.tenant_session(job.company_id) as s:
        try:
            repo = TenantRepo(s, job.company_id)
            resume = await repo.get(Resume, uuid.UUID(job.payload["resume_id"]), for_update=True)
        except NotFound:
            return
        if (
            resume.processing_generation == job.payload.get("generation", 1)
            and resume.processed_generation < resume.processing_generation
        ):
            resume.status = "failed"
            resume.status_reason = f"processing failed: {error[:300]}"
