"""Talent pool: semantic search across every candidate in a company.

Retrieval only. Results suggest candidates to consider; screening scores and eligibility always come from a normal
screening run. Vectors are built from redacted resume text (no contact details), stored per (resume, model), and
cascade with the resume and candidate, so erasure and retention remove them.
"""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import UTC, datetime
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from backend.db import Database, TenantRepo
from backend.models import Application, Candidate, CandidateEmbedding, Job, Resume
from backend.queue import ClaimedJob, Queue
from backend.security import Principal
from backend.services.common import ServiceError, audit, record_usage
from engine.config import ConfigError, Settings
from engine.config import get_settings as get_engine_settings
from engine.llm import EmbeddingResult, LLMError, get_embedder
from engine.logging_setup import log_event

logger = logging.getLogger("backend.talent_pool")


def embedding_model_id(settings: Settings) -> str:
    """Vectors from different models are never compared; the id names the provider and model."""
    return f"{settings.llm_provider}:{settings.embedding_model}"


def talent_pool_enabled(settings: Settings) -> bool:
    try:
        return settings.resolved_semantic_backend == "embeddings"
    except ConfigError:
        return False


async def enqueue_embedding(repo: TenantRepo, queue: Queue, resume_id: uuid.UUID, *, attempt: str = "") -> bool:
    """Queue the resume's vector for the current model. False when embeddings are not configured.

    Idempotent per ``attempt``: a reindex passes a new one so a resume whose vector was removed is queued again
    (the job itself skips resumes that already have a vector).
    """
    settings = get_engine_settings()
    if not talent_pool_enabled(settings):
        return False
    model = embedding_model_id(settings)
    await queue.enqueue(
        repo.session,
        company_id=repo.company_id,
        kind="embed_resume",
        payload={"resume_id": str(resume_id), "model": model},
        key=f"embed_resume:{resume_id}:{model}{':' + attempt if attempt else ''}",
    )
    return True


def _unit(vector: list[float]) -> list[float]:
    norm = math.hypot(*vector)
    if not norm or not math.isfinite(norm):
        raise LLMError("Embedding model returned a zero or invalid vector")
    return [value / norm for value in vector]


async def process_embed_resume(db: Database, job: ClaimedJob) -> None:
    resume_id = uuid.UUID(job.payload["resume_id"])
    settings = get_engine_settings()
    model = embedding_model_id(settings)
    if job.payload.get("model") != model:
        return  # configuration changed since queuing; a reindex queues the current model
    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        resume = await repo.get(Resume, resume_id)
        if resume.status != "ready" or resume.candidate_id is None or not resume.redacted_text:
            return
        done = await repo.find(
            CandidateEmbedding, CandidateEmbedding.resume_id == resume_id, CandidateEmbedding.model == model
        )
        if done is not None:
            return
        text = resume.redacted_text

    # The model call runs outside any transaction or lock.
    result: EmbeddingResult = await asyncio.to_thread(get_embedder(settings).embed, [text])
    vector = _unit(result.vectors[0])

    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        resume = await repo.get(Resume, resume_id)
        if resume.candidate_id is None:
            return
        # Erasure holds this row lock: a candidate deleted meanwhile raises NotFound and nothing is written.
        await repo.get(Candidate, resume.candidate_id, for_update=True)
        await s.execute(
            insert(CandidateEmbedding)
            .values(
                id=uuid.uuid4(),
                company_id=job.company_id,
                candidate_id=resume.candidate_id,
                resume_id=resume.id,
                model=model,
                dimension=len(vector),
                vector=vector,
            )
            .on_conflict_do_nothing(index_elements=["resume_id", "model"])
        )
        record_usage(repo, "embedding", prompt_tokens=result.usage.prompt_tokens, cost_usd=result.usage.cost_usd or 0.0)
    log_event(logger, "resume_embedded", resume_id=str(resume_id), dimension=len(vector))


async def reindex(repo: TenantRepo, principal: Principal, queue: Queue) -> int:
    """Queue vectors for every ready resume that has none for the current model (e.g. after an upgrade)."""
    settings = get_engine_settings()
    if not talent_pool_enabled(settings):
        raise ServiceError("Talent-pool search needs SEMANTIC_BACKEND=embeddings", status=409)
    model = embedding_model_id(settings)
    indexed = select(CandidateEmbedding.resume_id).where(
        CandidateEmbedding.company_id == repo.company_id, CandidateEmbedding.model == model
    )
    missing = (
        (
            await repo.session.execute(
                select(Resume.id).where(
                    Resume.company_id == repo.company_id,
                    Resume.status == "ready",
                    Resume.candidate_id.is_not(None),
                    Resume.id.not_in(indexed),
                )
            )
        )
        .scalars()
        .all()
    )
    attempt = uuid.uuid4().hex
    for resume_id in missing:
        await enqueue_embedding(repo, queue, resume_id, attempt=attempt)
    audit(repo, principal, "talent_pool.reindexed", "company", repo.company_id, after={"queued": len(missing)})
    return len(missing)


async def _embed_query(settings: Settings, text: str) -> EmbeddingResult:
    embedder = get_embedder(settings)
    # Some local models expect a different task prefix for queries; cloud embeddings have one method.
    method = getattr(embedder, "embed_query", embedder.embed)
    try:
        return await asyncio.to_thread(method, [text])
    except LLMError as exc:
        raise ServiceError("The embedding model is unavailable; start the local AI server", status=503) from exc


def _rank(rows: list[Any], query: np.ndarray, limit: int) -> list[tuple[uuid.UUID, uuid.UUID, float]]:
    """Cosine similarity, best resume per candidate, highest first."""
    rows = [row for row in rows if len(row.vector) == len(query)]
    if not rows:
        return []
    matrix = np.asarray([row.vector for row in rows], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    scores = (matrix @ query) / norms
    best: dict[uuid.UUID, tuple[uuid.UUID, float]] = {}
    for index in np.argsort(-scores):
        row = rows[int(index)]
        if row.candidate_id not in best:
            best[row.candidate_id] = (row.resume_id, float(scores[index]))
            if len(best) == limit:
                break
    return [(candidate_id, resume_id, score) for candidate_id, (resume_id, score) in best.items()]


async def search(
    repo: TenantRepo,
    principal: Principal,
    *,
    query: str | None,
    job_id: uuid.UUID | None,
    limit: int,
) -> dict[str, Any]:
    settings = get_engine_settings()
    if not talent_pool_enabled(settings):
        raise ServiceError("Talent-pool search needs SEMANTIC_BACKEND=embeddings", status=409)
    model = embedding_model_id(settings)
    job = await repo.get(Job, job_id) if job_id else None
    text = query or f"{job.title}\n{job.jd_text}"  # type: ignore[union-attr]  # one of them is validated present

    embedded = await _embed_query(settings, text)
    vector = np.asarray(_unit(embedded.vectors[0]), dtype=np.float32)
    record_usage(repo, "embedding", prompt_tokens=embedded.usage.prompt_tokens, cost_usd=embedded.usage.cost_usd or 0.0)

    rows = (
        await repo.session.execute(
            select(CandidateEmbedding.candidate_id, CandidateEmbedding.resume_id, CandidateEmbedding.vector).where(
                CandidateEmbedding.company_id == repo.company_id, CandidateEmbedding.model == model
            )
        )
    ).all()
    ranked = _rank(rows, vector, limit)
    indexed = len({row.candidate_id for row in rows})
    pending = (
        await repo.session.execute(
            select(func.count())
            .select_from(Resume)
            .where(
                Resume.company_id == repo.company_id,
                Resume.status == "ready",
                Resume.candidate_id.is_not(None),
                Resume.id.not_in(
                    select(CandidateEmbedding.resume_id).where(
                        CandidateEmbedding.company_id == repo.company_id, CandidateEmbedding.model == model
                    )
                ),
            )
        )
    ).scalar_one()

    ids = [candidate_id for candidate_id, _, _ in ranked]
    applications: dict[uuid.UUID, list[Application]] = {candidate_id: [] for candidate_id in ids}
    if ids:
        for app in await repo.list(Application, Application.candidate_id.in_(ids)):
            applications[app.candidate_id].append(app)
    resumes = {r.id: r for r in await repo.list(Resume, Resume.id.in_([r for _, r, _ in ranked]))} if ids else {}
    results = []
    for candidate_id, resume_id, similarity in ranked:
        resume = resumes.get(resume_id)
        in_job = next((a for a in applications[candidate_id] if job_id and a.job_id == job_id), None)
        results.append(
            {
                "candidate_id": str(candidate_id),
                "similarity": round(similarity, 4),
                "matched_resume": {
                    "resume_id": str(resume_id),
                    "version": resume.version if resume else None,
                    "created_at": resume.created_at.isoformat() if resume else None,
                },
                "applications": len(applications[candidate_id]),
                "in_job": {"application_id": str(in_job.id), "stage": in_job.stage} if in_job else None,
            }
        )
    log_event(logger, "talent_pool_searched", results=len(results), indexed=indexed, by_job=bool(job_id))
    return {
        "model": model,
        "query_source": "query" if query else "job_description",
        "job_id": str(job_id) if job_id else None,
        "indexed_candidates": indexed,
        "resumes_pending_index": pending,
        "results": results,
    }


async def add_to_job(repo: TenantRepo, principal: Principal, job_id: uuid.UUID, candidate_id: uuid.UUID) -> tuple:
    """Apply an existing candidate to a job with their latest ready resume. Returns (application, created)."""
    await repo.get(Job, job_id)
    candidate = await repo.get(Candidate, candidate_id, for_update=True)
    existing = await repo.find(Application, Application.job_id == job_id, Application.candidate_id == candidate_id)
    if existing is not None:
        return existing, False
    latest = await repo.list(
        Resume,
        Resume.candidate_id == candidate_id,
        Resume.status == "ready",
        order_by=[Resume.version.desc()],
        limit=1,
    )
    if not latest:
        raise ServiceError("This candidate has no processed resume to apply with")
    app = repo.add(Application(job_id=job_id, candidate_id=candidate_id, resume_id=latest[0].id))
    candidate.last_activity_at = datetime.now(UTC)
    await repo.flush()
    audit(
        repo,
        principal,
        "application.added_from_talent_pool",
        "application",
        app.id,
        after={"job_id": str(job_id), "candidate_id": str(candidate_id), "resume_id": str(latest[0].id)},
    )
    return app, True
