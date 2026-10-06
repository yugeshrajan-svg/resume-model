"""Talent-pool matching: which consenting pool candidates are eligible for a confirmed requirements version.

Confirming a version queues ``match_pool``. It selects candidates with an active ``talent_pool`` consent and a
processed resume who have not applied to the job, then queues one ``pool_check`` per candidate. A check applies the
same extraction (shared company cache) and knockout rules as screening. Results are suggestions for HR: nothing here
creates an application. Rows cascade with the candidate, resume, job and requirements version.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from backend.db import Database, NotFound, TenantRepo
from backend.models import (
    Application,
    Candidate,
    CandidateContact,
    CandidateEmbedding,
    Consent,
    ExtractionCacheEntry,
    Job,
    JobRequirementsVersion,
    PoolMatch,
    Resume,
)
from backend.queue import ClaimedJob, Queue
from backend.security import Principal
from backend.services.common import ServiceError, audit, record_usage
from backend.services.screening import extraction_entry_key, latest_confirmed_requirements, requirements_for_engine
from backend.services.talent_pool import _embed_query, _unit, embedding_model_id, talent_pool_enabled
from backend.snapshots import engine_fingerprint
from engine.config import Settings
from engine.config import get_settings as get_engine_settings
from engine.extraction import extract_candidate
from engine.knockout import evaluate_knockouts
from engine.llm import get_llm
from engine.logging_setup import log_event
from engine.normalize import load_ontology, normalize_candidate
from engine.schemas import ExtractionResult, JobRequirements

logger = logging.getLogger("backend.pool_matching")
POOL_SCOPE = "talent_pool"


def _pool_consent(company_id: uuid.UUID):
    return select(Consent.candidate_id).where(
        Consent.company_id == company_id,
        Consent.scope == POOL_SCOPE,
        Consent.revoked_at.is_(None),
        Consent.granted_at <= datetime.now(UTC),
    )


async def has_pool_consent(repo: TenantRepo, candidate_id: uuid.UUID) -> bool:
    found = await repo.session.execute(_pool_consent(repo.company_id).where(Consent.candidate_id == candidate_id))
    return found.first() is not None


async def enqueue_match(repo: TenantRepo, queue: Queue, version: JobRequirementsVersion, *, attempt: str = "") -> None:
    await queue.enqueue(
        repo.session,
        company_id=repo.company_id,
        kind="match_pool",
        payload={"job_id": str(version.job_id), "requirements_id": str(version.id)},
        key=f"match_pool:{version.id}{':' + attempt if attempt else ''}",
    )


async def refresh(repo: TenantRepo, principal: Principal, queue: Queue, job_id: uuid.UUID) -> int:
    """Check the pool again for the latest confirmed version (e.g. after new candidates joined the pool)."""
    await repo.get(Job, job_id)
    version = await latest_confirmed_requirements(repo, job_id)
    if version is None:
        raise ServiceError("Confirm the job's requirements before matching the talent pool")
    await enqueue_match(repo, queue, version, attempt=uuid.uuid4().hex)
    audit(repo, principal, "pool_matches.refreshed", "job", job_id, after={"version": version.version})
    return version.version


async def _similarities(
    db: Database, company_id: uuid.UUID, settings: Settings, text: str, resume_ids: list[uuid.UUID]
) -> dict[uuid.UUID, float]:
    """Semantic closeness of each resume to the job, for ordering. Empty when embeddings are unavailable."""
    if not resume_ids or not talent_pool_enabled(settings):
        return {}
    try:
        embedded = await _embed_query(settings, text)
    except ServiceError:
        return {}  # the eligibility checks do not depend on the embedding server
    query = np.asarray(_unit(embedded.vectors[0]), dtype=np.float32)
    async with db.tenant_session(company_id) as s:
        repo = TenantRepo(s, company_id)
        record_usage(repo, "embedding", prompt_tokens=embedded.usage.prompt_tokens, cost_usd=embedded.usage.cost_usd)
        rows = (
            await s.execute(
                select(CandidateEmbedding.resume_id, CandidateEmbedding.vector).where(
                    CandidateEmbedding.company_id == company_id,
                    CandidateEmbedding.model == embedding_model_id(settings),
                    CandidateEmbedding.resume_id.in_(resume_ids),
                )
            )
        ).all()
    out = {}
    for resume_id, vector in rows:
        values = np.asarray(vector, dtype=np.float32)
        if len(values) == len(query) and (norm := float(np.linalg.norm(values))):
            out[resume_id] = round(float(values @ query) / norm, 4)
    return out


async def process_match_pool(db: Database, queue: Queue, job: ClaimedJob) -> None:
    requirements_id = uuid.UUID(job.payload["requirements_id"])
    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        version = await repo.get(JobRequirementsVersion, requirements_id)
        if version.confirmed_at is None:
            return
        target = await repo.get(Job, version.job_id)
        text = f"{target.title}\n{target.jd_text}"
        applied = select(Application.candidate_id).where(
            Application.company_id == job.company_id, Application.job_id == version.job_id
        )
        rows = (
            await s.execute(
                select(Resume.candidate_id, Resume.id)
                .where(
                    Resume.company_id == job.company_id,
                    Resume.status == "ready",
                    Resume.candidate_id.in_(_pool_consent(job.company_id)),
                    Resume.candidate_id.not_in(applied),
                )
                .order_by(Resume.candidate_id, Resume.version.desc())
            )
        ).all()
    latest: dict[uuid.UUID, uuid.UUID] = {}
    for candidate_id, resume_id in rows:
        latest.setdefault(candidate_id, resume_id)  # newest version first per candidate
    similarity = await _similarities(db, job.company_id, get_engine_settings(), text, list(latest.values()))

    queued = 0
    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        existing = {m.candidate_id: m for m in await repo.list(PoolMatch, PoolMatch.requirements_id == requirements_id)}
        for candidate_id, resume_id in latest.items():
            match = existing.get(candidate_id)
            if match is None:
                match = repo.add(
                    PoolMatch(
                        job_id=version.job_id,
                        requirements_id=requirements_id,
                        requirements_version=version.version,
                        candidate_id=candidate_id,
                        resume_id=resume_id,
                        status="pending",
                    )
                )
                await repo.flush()
            elif match.resume_id != resume_id or match.status == "failed":
                match.resume_id, match.status, match.eligible = resume_id, "pending", None
                match.knockout = match.failure_reason = match.checked_at = None
            match.similarity = similarity.get(resume_id)
            if match.status == "pending":
                await queue.enqueue(
                    s,
                    company_id=job.company_id,
                    kind="pool_check",
                    payload={"match_id": str(match.id), "candidate_id": str(candidate_id), "resume_id": str(resume_id)},
                    key=f"pool_check:{match.id}:{resume_id}:{job.id}",
                )
                queued += 1
        audit(
            repo,
            "system:worker",
            "pool_matches.queued",
            "job",
            version.job_id,
            after={"version": version.version, "candidates": len(latest), "checks_queued": queued},
        )
    log_event(logger, "pool_match_queued", candidates=len(latest), checks=queued)


def _check(
    requirements: JobRequirements, text: str, cached: ExtractionResult | None, llm: Any, settings: Settings
) -> tuple[ExtractionResult, dict[str, Any] | None]:
    """Extraction (unless cached) and knockouts only; runs in a worker thread."""
    ontology = load_ontology()
    extraction = cached or extract_candidate(text, llm, settings=settings, ontology=ontology)
    if extraction.status != "ok" or extraction.extraction is None:
        return extraction, None
    candidate = normalize_candidate(
        extraction.extraction, ontology, date.today(), low_confidence_jobs=extraction.low_confidence_jobs
    )
    knockout = evaluate_knockouts(requirements, candidate, settings, ontology)
    return extraction, knockout.model_dump(mode="json")


async def process_pool_check(db: Database, job: ClaimedJob) -> None:
    match_id = uuid.UUID(job.payload["match_id"])
    settings = get_engine_settings()
    llm = get_llm(settings) if settings.resolved_extraction_mode == "llm" else None
    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        match = await repo.get(PoolMatch, match_id)
        candidate_id = match.candidate_id
        if match.status == "checked":
            return
        if not await has_pool_consent(repo, candidate_id):
            await s.delete(match)  # consent withdrawn: nothing is kept or checked
            return
        requirements = requirements_for_engine(await repo.get(JobRequirementsVersion, match.requirements_id))
        resume = await repo.get(Resume, match.resume_id)
        text, content_hash = resume.redacted_text or "", resume.content_hash
        key = extraction_entry_key(text, llm, settings, engine_fingerprint())
        entry = await repo.find(ExtractionCacheEntry, ExtractionCacheEntry.cache_key == key)
    cached = ExtractionResult.model_validate(entry.result).model_copy(update={"from_cache": True}) if entry else None

    extraction, knockout = await asyncio.to_thread(_check, requirements, text, cached, llm, settings)

    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        await repo.get(Candidate, candidate_id, for_update=True)  # erasure holds this lock
        match = await repo.get(PoolMatch, match_id, for_update=True)
        if not await has_pool_consent(repo, candidate_id):
            await s.delete(match)
            return
        if cached is None and extraction.status == "ok" and settings.extraction_cache_enabled:
            await s.execute(
                insert(ExtractionCacheEntry)
                .values(
                    company_id=job.company_id,
                    cache_key=key,
                    content_hash=content_hash,
                    result=extraction.model_dump(mode="json"),
                )
                .on_conflict_do_nothing()
            )
        if cached is None and extraction.usage:
            u = extraction.usage
            record_usage(
                repo, "llm", prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens, cost_usd=u.cost_usd
            )
        match.checked_at = datetime.now(UTC)
        if knockout is None:
            match.status, match.eligible = "failed", None
            match.failure_reason = (extraction.failure_reason or "resume could not be read")[:500]
        else:
            match.status, match.eligible, match.knockout = "checked", knockout["eligible"], knockout
            match.failure_reason = None


async def pool_matches(
    repo: TenantRepo, principal: Principal, job_id: uuid.UUID, version: int | None = None
) -> dict[str, Any]:
    await repo.get(Job, job_id)
    if version is None:
        chosen = await latest_confirmed_requirements(repo, job_id)
    else:
        chosen = await repo.find(
            JobRequirementsVersion, JobRequirementsVersion.job_id == job_id, JobRequirementsVersion.version == version
        )
        if chosen is None:
            raise NotFound("requirements version not found")
    consenting = set((await repo.session.execute(_pool_consent(repo.company_id))).scalars())
    empty = {"total": 0, "checked": 0, "pending": 0, "failed": 0}
    if chosen is None or chosen.confirmed_at is None:
        return {
            "job_id": str(job_id),
            "requirements_version": chosen.version if chosen else None,
            "confirmed": False,
            "pool_size": len(consenting),
            "progress": empty,
            "eligible": [],
            "ineligible": [],
            "failed": [],
        }
    rows = [
        m for m in await repo.list(PoolMatch, PoolMatch.requirements_id == chosen.id) if m.candidate_id in consenting
    ]
    stages = {
        a.candidate_id: a.stage
        for a in await repo.list(Application, Application.job_id == job_id)
        if a.candidate_id in {m.candidate_id for m in rows}
    }
    names: dict[uuid.UUID, str | None] = {}
    if principal.can("contacts:read") and rows:
        contacts = await repo.list(CandidateContact, CandidateContact.candidate_id.in_([m.candidate_id for m in rows]))
        names = {c.candidate_id: c.name for c in contacts}

    def out(m: PoolMatch) -> dict[str, Any]:
        item: dict[str, Any] = {
            "match_id": str(m.id),
            "candidate_id": str(m.candidate_id),
            "resume_id": str(m.resume_id),
            "similarity": m.similarity,
            "status": m.status,
            "eligible": m.eligible,
            "reasons": [r["detail"] for r in (m.knockout or {}).get("reasons", [])],
            "warnings": (m.knockout or {}).get("warnings", []),
            "failure_reason": m.failure_reason,
            "checked_at": m.checked_at.isoformat() if m.checked_at else None,
            "in_job": stages.get(m.candidate_id),
        }
        if principal.can("contacts:read"):
            item["name"] = names.get(m.candidate_id)
        return item

    def by_fit(m: PoolMatch) -> tuple[bool, float]:
        return (m.similarity is None, -(m.similarity or 0.0))

    progress = {"total": len(rows), **{k: sum(m.status == k for m in rows) for k in ("checked", "pending", "failed")}}
    return {
        "job_id": str(job_id),
        "requirements_version": chosen.version,
        "confirmed": True,
        "pool_size": len(consenting),
        "progress": progress,
        "eligible": [out(m) for m in sorted((m for m in rows if m.eligible), key=by_fit)],
        "ineligible": [out(m) for m in sorted((m for m in rows if m.eligible is False), key=by_fit)],
        "failed": [out(m) for m in rows if m.status == "failed"],
    }
