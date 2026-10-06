"""Screening runs: creation (latest confirmed requirements, consent gate) and the screen_application job.

The screening path receives exactly two inputs per application: the confirmed JobRequirements and the resume's
``redacted_text`` (contact details removed at ingestion). It never reads candidate_contacts.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from backend.db import Database, TenantRepo
from backend.models import (
    Application,
    Candidate,
    Consent,
    ExtractionCacheEntry,
    Job,
    JobRequirementsVersion,
    Resume,
    ResumeRegistration,
    ScreeningInput,
    ScreeningResult,
    ScreeningRun,
)
from backend.queue import ClaimedJob, Queue
from backend.security import Principal
from backend.services.common import ServiceError, audit, record_usage
from backend.services.screening_sources import ResumeSource, consenting_candidates, load_sources
from backend.snapshots import engine_fingerprint, freeze_settings, restore_settings, validate_snapshot
from engine import __version__ as ENGINE_VERSION
from engine.cache import extraction_cache_key
from engine.config import Settings
from engine.config import get_settings as get_engine_settings
from engine.explain import explain
from engine.extraction import extract_candidate
from engine.features import build_context, compute_features, job_profile_text
from engine.knockout import evaluate_knockouts
from engine.llm import LLMError, LLMProvider, get_llm
from engine.logging_setup import log_event
from engine.normalize import date_warnings, load_ontology, normalize_candidate
from engine.prompts import get_prompt
from engine.schemas import ExtractionResult, JobRequirements, LLMUsage
from engine.scorer import resolve_weights, score_features
from engine.semantic import build_reranker, build_semantic_scorer

logger = logging.getLogger("backend.screening")


async def latest_confirmed_requirements(repo: TenantRepo, job_id: uuid.UUID) -> JobRequirementsVersion | None:
    return (
        await repo.session.execute(
            repo.select(
                JobRequirementsVersion,
                JobRequirementsVersion.job_id == job_id,
                JobRequirementsVersion.confirmed_at.is_not(None),
            )
            .order_by(JobRequirementsVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def requirements_for_engine(version: JobRequirementsVersion) -> JobRequirements:
    if version.confirmed_at is None or version.confirmed_by is None:
        raise ServiceError("Requirements version is not confirmed")
    data = {
        **version.requirements,
        "requirements_confirmed": {"by": str(version.confirmed_by), "at": version.confirmed_at},
    }
    return JobRequirements.model_validate(data)


async def has_consent(repo: TenantRepo, candidate_id: uuid.UUID) -> bool:
    return (
        await repo.find(
            Consent,
            Consent.candidate_id == candidate_id,
            Consent.revoked_at.is_(None),
            Consent.granted_at <= datetime.now(UTC),
        )
    ) is not None


async def has_input_consent(
    repo: TenantRepo, candidate_id: uuid.UUID, run: ScreeningRun, frozen: ScreeningInput | None
) -> bool:
    """Saved-only inputs retain their talent-pool consent requirement during execution and retries."""
    source = (run.snapshot or {}).get("resume_source")
    needs_pool = source == "database"
    if source == "both" and frozen is not None:
        cutoff = datetime.fromisoformat(run.snapshot["source_selected_at"])
        uploaded = await repo.find(
            ResumeRegistration,
            ResumeRegistration.resume_id == frozen.resume_id,
            ResumeRegistration.job_id == run.job_id,
            ResumeRegistration.created_at <= cutoff,
        )
        resume = await repo.get(Resume, frozen.resume_id)
        needs_pool = uploaded is None and resume.pending_job_id != run.job_id
    if needs_pool:
        return (
            await repo.session.execute(
                consenting_candidates(repo.company_id, pool=True).where(Consent.candidate_id == candidate_id)
            )
        ).first() is not None
    return await has_consent(repo, candidate_id)


async def create_run(
    repo: TenantRepo, principal: Principal, queue: Queue, job_id: uuid.UUID, *, source: ResumeSource | None = None
) -> ScreeningRun:
    """Screen every application of the job against the latest confirmed requirements.

    Applications whose candidate has no consent record, or whose resume is not ready, are excluded and listed
    in ``run.blocked`` with a reason. If nothing can be screened the request fails with that list.
    """
    await repo.get(Job, job_id, for_update=True)
    version = await latest_confirmed_requirements(repo, job_id)
    if version is None:
        raise ServiceError("This job has no confirmed requirements; confirm a requirements version first")
    requirements = requirements_for_engine(version)
    engine_settings = get_engine_settings()
    local_prompt_version = None
    if engine_settings.llm_provider == "ollama":
        from engine.local_ai import LOCAL_PROMPT_VERSION, pin_local_models

        local_prompt_version = LOCAL_PROMPT_VERSION

        try:
            engine_settings = await asyncio.to_thread(pin_local_models, engine_settings)
        except LLMError as exc:
            raise ServiceError(str(exc), status=503) from exc

    apps = await repo.list(Application, Application.job_id == job_id, order_by=[Application.candidate_id])
    source_selected_at = datetime.now(UTC)
    sources = await load_sources(repo, job_id) if source else None
    selected = sources.selected(source) if sources and source else {a.candidate_id: a.resume_id for a in apps}
    if sources and source in {"uploads", "both"} and sources.pending_uploads:
        raise ServiceError("Wait for uploaded resumes to finish processing before screening")
    applications = {a.candidate_id: a for a in apps}
    screenable: list[Application] = []
    blocked: list[dict[str, Any]] = []
    inputs = []
    added_from_database = 0
    for candidate_id, resume_id in sorted(selected.items()):
        candidate = await repo.get(Candidate, candidate_id, for_update=True)
        resume = await repo.get(Resume, resume_id)
        app = applications.get(candidate_id)
        pool_required = bool(sources and source and sources.requires_pool_consent(source, candidate_id))
        pool_allowed = (
            not pool_required
            or (
                await repo.session.execute(
                    consenting_candidates(repo.company_id, pool=True).where(Consent.candidate_id == candidate_id)
                )
            ).first()
            is not None
        )
        if not pool_allowed:
            reason = "talent-pool consent is required to use this saved resume for a new job"
        elif not await has_consent(repo, candidate_id):
            reason = "no consent record: record the candidate's consent (source and purpose) before screening"
        elif resume.status != "ready":
            reason = f"resume not ready ({resume.status}: {resume.status_reason or 'processing'})"
        else:
            if app is None:
                app = repo.add(Application(job_id=job_id, candidate_id=candidate_id, resume_id=resume.id))
                await repo.flush()
                added_from_database += 1
            elif source and app.resume_id != resume.id:
                app = await repo.get(Application, app.id, for_update=True)
                app.resume_id = resume.id
                if app.stage == "screened":
                    app.stage = "applied"
            screenable.append(app)
            candidate.last_activity_at = datetime.now(UTC)
            inputs.append(
                ScreeningInput(
                    company_id=repo.company_id,
                    application_id=app.id,
                    candidate_id=app.candidate_id,
                    resume_id=resume.id,
                    resume_version=resume.version,
                    processing_generation=resume.processed_generation,
                    content_hash=resume.content_hash,
                    redacted_text=resume.redacted_text or "",
                )
            )
            continue
        blocked.append(
            {"application_id": str(app.id) if app else None, "candidate_id": str(candidate_id), "reason": reason}
        )
    if not screenable:
        raise ServiceError(
            "No applications can be screened", details={"blocked": blocked, "source": source, "selected": len(selected)}
        )

    llm_mode = engine_settings.resolved_extraction_mode
    run = repo.add(
        ScreeningRun(
            id=uuid.uuid4(),
            job_id=job_id,
            job_requirements_id=version.id,
            requirements_version=version.version,
            extraction_mode=llm_mode,
            semantic_backend=engine_settings.resolved_semantic_backend,
            versions={
                "engine": ENGINE_VERSION,
                "resume_prompt": get_prompt("resume_extraction").version if llm_mode == "llm" else None,
                "explanation_prompt": get_prompt("explanation").version if llm_mode == "llm" else None,
                "provider": engine_settings.llm_provider,
                "provider_prompt_version": local_prompt_version,
                "model_digest": engine_settings.ollama_chat_digest if llm_mode == "llm" else None,
                "embedding_digest": engine_settings.ollama_embedding_digest
                if engine_settings.resolved_semantic_backend == "embeddings"
                else None,
                "model": engine_settings.chat_model if llm_mode == "llm" else None,
                "embedding_model": engine_settings.embedding_model
                if engine_settings.resolved_semantic_backend == "embeddings"
                else None,
                "reranker_model": engine_settings.reranker_model
                if engine_settings.semantic_reranker != "none"
                else None,
                "ontology": load_ontology().version,
            },
            weights=resolve_weights(requirements, engine_settings),
            status="queued",
            total=len(screenable),
            blocked=blocked,
            created_by=principal.user_id,
        )
    )
    run.reference_date = datetime.now(UTC).date()
    run.snapshot = {
        "format": 1,
        "fingerprint": engine_fingerprint(),
        "requirements": requirements.model_dump(mode="json"),
        "settings": freeze_settings(engine_settings),
    }
    if source:
        run.snapshot["resume_source"] = source
        run.snapshot["source_selected_at"] = source_selected_at.isoformat()
        run.snapshot["source_counts"] = {
            "database": len(sources.database),
            "uploads": len(sources.uploads),
            "selected": len(selected),
            "added_from_database": added_from_database,
        }
    await repo.flush()
    for frozen in inputs:
        frozen.run_id = run.id
        repo.add(frozen)
        await queue.enqueue(
            repo.session,
            company_id=repo.company_id,
            kind="screen_application",
            payload={"run_id": str(run.id), "application_id": str(frozen.application_id)},
            key=f"screen_application:{run.id}:{frozen.application_id}:{version.version}",
        )
    audit(
        repo,
        principal,
        "screening_run.created",
        "screening_run",
        run.id,
        after={"job_id": str(job_id), "requirements_version": version.version, "total": run.total,
               "blocked": len(blocked)},
    )  # fmt: skip
    return run


async def retry_run(repo: TenantRepo, principal: Principal, queue: Queue, run_id: uuid.UUID) -> ScreeningRun:
    original = await repo.get(ScreeningRun, run_id, for_update=True)
    if original.status != "completed":
        raise ServiceError("Wait for the original run to complete before retrying failed applications")
    try:
        validate_snapshot(original.snapshot)
    except RuntimeError as exc:
        raise ServiceError("Original worker version is unavailable; create a new screening run") from exc
    failed = await repo.list(
        ScreeningResult, ScreeningResult.run_id == run_id, ScreeningResult.status == "needs_reprocess"
    )
    inputs, blocked = [], []
    for result in sorted(failed, key=lambda result: str(result.candidate_id)):
        await repo.get(Candidate, result.candidate_id, for_update=True)
        frozen = await repo.find(
            ScreeningInput, ScreeningInput.run_id == run_id, ScreeningInput.application_id == result.application_id
        )
        if not await has_input_consent(repo, result.candidate_id, original, frozen):
            blocked.append(
                {
                    "application_id": str(result.application_id),
                    "candidate_id": str(result.candidate_id),
                    "reason": "no consent record",
                }
            )
            continue
        if frozen is not None:
            inputs.append(frozen)
    if not inputs:
        raise ServiceError("No failed applications can be retried", details={"blocked": blocked})
    run = repo.add(
        ScreeningRun(
            id=uuid.uuid4(),
            job_id=original.job_id,
            job_requirements_id=original.job_requirements_id,
            requirements_version=original.requirements_version,
            extraction_mode=original.extraction_mode,
            semantic_backend=original.semantic_backend,
            versions=copy.deepcopy(original.versions),
            weights=copy.deepcopy(original.weights),
            snapshot=copy.deepcopy(original.snapshot),
            reference_date=original.reference_date,
            retry_of_run_id=original.id,
            status="queued",
            total=len(inputs),
            blocked=blocked,
            created_by=principal.user_id,
        )
    )
    await repo.flush()
    for old in inputs:
        repo.add(
            ScreeningInput(
                run_id=run.id,
                application_id=old.application_id,
                candidate_id=old.candidate_id,
                resume_id=old.resume_id,
                resume_version=old.resume_version,
                processing_generation=old.processing_generation,
                content_hash=old.content_hash,
                redacted_text=old.redacted_text,
                extraction=copy.deepcopy(old.extraction),
            )
        )
        await queue.enqueue(
            repo.session,
            company_id=repo.company_id,
            kind="screen_application",
            payload={"run_id": str(run.id), "application_id": str(old.application_id)},
            key=f"screen_application:{run.id}:{old.application_id}:{run.requirements_version}",
        )
    audit(
        repo,
        principal,
        "screening_run.retried",
        "screening_run",
        run.id,
        after={"original_run_id": str(original.id), "total": run.total},
    )
    return run


def extraction_entry_key(text: str, llm: LLMProvider | None, settings: Settings, fingerprint: str) -> str:
    """Company extraction-cache key: the engine key plus the code fingerprint and model endpoint.

    Shared by screening runs and talent-pool checks, so a resume read for one is reused by the other.
    """
    base_key = extraction_cache_key(text, llm, settings, load_ontology())
    return hashlib.sha256(
        json.dumps(
            [base_key, fingerprint, settings.azure_openai_endpoint, settings.azure_openai_api_version],
            sort_keys=True,
        ).encode()
    ).hexdigest()


@dataclass
class _Assessment:
    extraction: ExtractionResult
    result: dict[str, Any]  # ScreeningResult columns
    usages: list[LLMUsage]


def assess(
    requirements: JobRequirements,
    redacted_text: str,
    cached: ExtractionResult | None,
    llm: LLMProvider | None,
    settings: Settings,
    today: date,
) -> _Assessment:
    """Pure engine work for one application (runs in a worker thread). Inputs: requirements + redacted text."""
    ontology = load_ontology()
    extraction = cached or extract_candidate(redacted_text, llm, settings=settings, ontology=ontology)
    usages = [extraction.usage] if extraction.usage and cached is None else []
    if extraction.status != "ok" or extraction.extraction is None:
        return _Assessment(
            extraction,
            {"status": "needs_reprocess", "reprocess_reason": extraction.failure_reason or "extraction failed"},
            usages,
        )
    candidate = normalize_candidate(
        extraction.extraction, ontology, today, low_confidence_jobs=extraction.low_confidence_jobs
    )
    knockout = evaluate_knockouts(requirements, candidate, settings, ontology)
    semantic = build_semantic_scorer(job_profile_text(requirements), settings, None, ontology)
    reranker = build_reranker(settings)
    features = compute_features(build_context(requirements, candidate, semantic, settings, ontology, reranker))
    breakdown = score_features(features, resolve_weights(requirements, settings))
    explanation = explain(
        features,
        knockout,
        llm,
        settings,
        warnings=[*date_warnings(candidate), *knockout.warnings],
        similarity_backends={
            "project_relevance": semantic.name,
            "semantic_relevance": reranker.name if reranker else semantic.name,
        },
    )
    if explanation.usage:
        usages.append(explanation.usage)
    if getattr(semantic, "usage_tokens", 0):
        usages.append(
            LLMUsage(
                provider=settings.llm_provider,
                model=settings.embedding_model,
                prompt_tokens=semantic.usage_tokens,
                completion_tokens=0,
                cost_usd=semantic.usage_cost_usd,
            )
        )
    return _Assessment(
        extraction,
        {
            "status": "scored",
            "score": breakdown.score,
            "eligible": knockout.eligible,
            "knockout": knockout.model_dump(mode="json"),
            "features": [f.model_dump(mode="json") for f in features],
            "score_breakdown": breakdown.model_dump(mode="json"),
            "explanation": explanation.model_dump(mode="json"),
            "explanation_source": explanation.method,
            "extraction_meta": {
                "method": extraction.method,
                "prompt_version": extraction.prompt_version,
                "model": extraction.model,
                "ontology_version": extraction.ontology_version,
                "evidence_drop_rates": extraction.evidence_drop_rates,
                "from_cache": cached.from_cache if cached is not None else False,
                "total_experience_months": candidate.total_experience_months,
            },
        },
        usages,
    )


async def process_screen_application(db: Database, job: ClaimedJob) -> None:
    start = time.perf_counter()
    run_id = uuid.UUID(job.payload["run_id"])
    app_id = uuid.UUID(job.payload["application_id"])

    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        if await repo.find(ScreeningResult, ScreeningResult.run_id == run_id, ScreeningResult.application_id == app_id):
            return  # already done (idempotent re-run)
        run = await repo.get(ScreeningRun, run_id)
        app = await repo.get(Application, app_id)
        frozen = await repo.find(
            ScreeningInput, ScreeningInput.run_id == run_id, ScreeningInput.application_id == app_id
        )
        consent = await has_input_consent(repo, app.candidate_id, run, frozen)
        candidate_id = app.candidate_id
        saved_snapshot, reference_date, weights = run.snapshot, run.reference_date, dict(run.weights)
        saved_extraction = frozen.extraction if frozen else None
        redacted_text, content_hash = (frozen.redacted_text, frozen.content_hash) if frozen else ("", "")
        mode = run.extraction_mode
        await s.execute(
            update(ScreeningRun)
            .where(
                ScreeningRun.id == run_id, ScreeningRun.company_id == job.company_id, ScreeningRun.status == "queued"
            )
            .values(status="running")
        )

    if not consent:  # consent withdrawn after the run was created
        no_consent = {"status": "needs_reprocess", "reprocess_reason": "no consent record"}
        await _store_result(db, job.company_id, run_id, app_id, candidate_id, no_consent, [], start)
        return

    snapshot = validate_snapshot(saved_snapshot)
    if frozen is None or reference_date is None:
        raise RuntimeError("Frozen run input unavailable; create a new run")
    engine_settings = restore_settings(snapshot["settings"], get_engine_settings())
    requirements = JobRequirements.model_validate(snapshot["requirements"]).model_copy(
        update={"score_weights": weights}
    )
    llm = get_llm(engine_settings) if mode == "llm" else None
    if mode == "llm" and llm is None:
        raise RuntimeError("run uses LLM extraction but the configured provider is unavailable")
    key = extraction_entry_key(redacted_text, llm, engine_settings, snapshot["fingerprint"])
    cached = (
        ExtractionResult.model_validate(saved_extraction).model_copy(update={"from_cache": True})
        if saved_extraction
        else None
    )
    if cached is None and engine_settings.extraction_cache_enabled:
        async with db.tenant_session(job.company_id) as s:
            entry = await TenantRepo(s, job.company_id).find(
                ExtractionCacheEntry, ExtractionCacheEntry.cache_key == key
            )
            cached = (
                ExtractionResult.model_validate(entry.result).model_copy(update={"from_cache": True}) if entry else None
            )
    fresh_usages = []
    if cached is None:
        cached = await asyncio.to_thread(
            extract_candidate, redacted_text, llm, settings=engine_settings, ontology=load_ontology()
        )
        fresh_usages = [cached.usage] if cached.usage else []
    if cached.status == "ok":
        async with db.tenant_session(job.company_id) as s:
            repo = TenantRepo(s, job.company_id)
            await repo.get(Candidate, candidate_id, for_update=True)
            if not await has_input_consent(repo, candidate_id, run, frozen):
                permitted = False
            else:
                permitted = True
                target = (
                    await s.execute(
                        repo.select(
                            ScreeningInput, ScreeningInput.run_id == run_id, ScreeningInput.application_id == app_id
                        ).with_for_update()
                    )
                ).scalar_one()
                if target.extraction:
                    cached = ExtractionResult.model_validate(target.extraction).model_copy(update={"from_cache": True})
                else:
                    target.extraction = cached.model_dump(mode="json")
                if engine_settings.extraction_cache_enabled:
                    await s.execute(
                        insert(ExtractionCacheEntry)
                        .values(
                            company_id=job.company_id,
                            cache_key=key,
                            content_hash=content_hash,
                            result=cached.model_dump(mode="json"),
                        )
                        .on_conflict_do_nothing()
                    )
                if fresh_usages:
                    pt, ct = sum(u.prompt_tokens for u in fresh_usages), sum(u.completion_tokens for u in fresh_usages)
                    cost = sum(u.cost_usd for u in fresh_usages)
                    record_usage(repo, "llm", run_id=run_id, prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)
                    await s.execute(
                        update(ScreeningRun)
                        .where(ScreeningRun.company_id == job.company_id, ScreeningRun.id == run_id)
                        .values(
                            prompt_tokens=ScreeningRun.prompt_tokens + pt,
                            completion_tokens=ScreeningRun.completion_tokens + ct,
                            cost_usd=ScreeningRun.cost_usd + cost,
                        )
                    )
        if not permitted:
            await _store_result(
                db,
                job.company_id,
                run_id,
                app_id,
                candidate_id,
                {"status": "needs_reprocess", "reprocess_reason": "no consent record"},
                fresh_usages,
                start,
            )
            return
        fresh_usages = []  # Metered at the durable extraction checkpoint; retries never charge it again.
    assessment = await asyncio.to_thread(
        assess, requirements, redacted_text, cached, llm, engine_settings, reference_date
    )
    await _store_result(
        db,
        job.company_id,
        run_id,
        app_id,
        candidate_id,
        assessment.result,
        [*fresh_usages, *assessment.usages],
        start,
    )


async def _store_result(
    db: Database,
    company_id: uuid.UUID,
    run_id: uuid.UUID,
    app_id: uuid.UUID,
    candidate_id: uuid.UUID,
    result: dict[str, Any],
    usages: list[LLMUsage],
    start: float,
) -> None:
    """Insert the result once; only the inserting worker advances the run's counters (idempotent)."""
    async with db.tenant_session(company_id) as s:
        repo = TenantRepo(s, company_id)
        # Consent grants/revocations use the same candidate lock. A withdrawal during an LLM call wins at commit.
        await repo.get(Candidate, candidate_id, for_update=True)
        run = await repo.get(ScreeningRun, run_id)
        frozen = await repo.find(
            ScreeningInput, ScreeningInput.run_id == run_id, ScreeningInput.application_id == app_id
        )
        if not await has_input_consent(repo, candidate_id, run, frozen):
            result = {"status": "needs_reprocess", "reprocess_reason": "no consent record"}
        inserted = (
            await s.execute(
                insert(ScreeningResult)
                .values(
                    id=uuid.uuid4(),
                    company_id=company_id,
                    run_id=run_id,
                    application_id=app_id,
                    candidate_id=candidate_id,
                    duration_ms=round((time.perf_counter() - start) * 1000, 1),
                    **result,
                )
                .on_conflict_do_nothing(index_elements=["run_id", "application_id"])
                .returning(ScreeningResult.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return
        pt = sum(u.prompt_tokens for u in usages)
        ct = sum(u.completion_tokens for u in usages)
        cost = round(sum(u.cost_usd for u in usages), 6)
        record_usage(repo, "llm", run_id=run_id, prompt_tokens=pt, completion_tokens=ct, cost_usd=cost)
        done, total = (
            await s.execute(
                update(ScreeningRun)
                .where(ScreeningRun.id == run_id, ScreeningRun.company_id == company_id)
                .values(
                    done=ScreeningRun.done + 1,
                    prompt_tokens=ScreeningRun.prompt_tokens + pt,
                    completion_tokens=ScreeningRun.completion_tokens + ct,
                    cost_usd=ScreeningRun.cost_usd + cost,
                    status="running",
                )
                .returning(ScreeningRun.done, ScreeningRun.total)
            )
        ).one()
        if done >= total:
            await s.execute(
                update(ScreeningRun)
                .where(ScreeningRun.id == run_id, ScreeningRun.company_id == company_id)
                .values(status="completed", finished_at=func.now())
            )
        if result["status"] == "scored" and frozen is not None:
            # One conditional statement: a manual decision committed at any moment is never overwritten.
            await s.execute(
                update(Application)
                .where(
                    Application.company_id == company_id,
                    Application.id == app_id,
                    Application.stage == "applied",
                    Application.resume_id == frozen.resume_id,
                )
                .values(stage="screened")
            )
    log_event(logger, "application_screened", run_id=str(run_id), status=result["status"], score=result.get("score"))


async def record_dead_screen_job(db: Database, job: ClaimedJob, error: str) -> None:
    """A screen_application job exhausted its retries: record it so the run can still complete."""
    run_id = uuid.UUID(job.payload["run_id"])
    app_id = uuid.UUID(job.payload["application_id"])
    async with db.tenant_session(job.company_id) as s:
        repo = TenantRepo(s, job.company_id)
        app = await repo.find(Application, Application.id == app_id)
        if app is None or await repo.find(ScreeningRun, ScreeningRun.id == run_id) is None:
            return
        candidate_id = app.candidate_id
    await _store_result(
        db, job.company_id, run_id, app_id, candidate_id,
        {"status": "needs_reprocess", "reprocess_reason": f"processing failed: {error[:300]}"}, [], time.perf_counter(),
    )  # fmt: skip


async def ranked_results(
    repo: TenantRepo,
    run_id: uuid.UUID,
    *,
    eligible: bool | None,
    stage: str | None,
    score_min: float | None,
    score_max: float | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int]:
    """Results of a run with a global rank among eligible candidates, then filtered and paginated in SQL."""
    rank = func.row_number().over(
        partition_by=ScreeningResult.eligible, order_by=(ScreeningResult.score.desc(), ScreeningResult.candidate_id)
    )
    base = (
        select(ScreeningResult, Application.stage.label("stage"), rank.label("rank"))
        .join(Application, Application.id == ScreeningResult.application_id)
        .where(
            ScreeningResult.company_id == repo.company_id,
            Application.company_id == repo.company_id,
            ScreeningResult.run_id == run_id,
        )
        .subquery()
    )
    filters = []
    if eligible is not None:
        filters.append(base.c.eligible.is_(eligible))
    if stage is not None:
        filters.append(base.c.stage == stage)
    if score_min is not None:
        filters.append(base.c.score >= score_min)
    if score_max is not None:
        filters.append(base.c.score <= score_max)
    total = (await repo.session.execute(select(func.count()).select_from(base).where(*filters))).scalar_one()
    rows = (
        await repo.session.execute(
            select(base)
            .where(*filters)
            .order_by(base.c.eligible.desc().nulls_last(), base.c.score.desc().nulls_last(), base.c.candidate_id)
            .limit(limit)
            .offset(offset)
        )
    ).mappings()
    items = []
    for r in rows:
        items.append(
            {
                "application_id": r["application_id"],
                "candidate_id": r["candidate_id"],
                "rank": r["rank"] if r["eligible"] else None,
                "score": r["score"],
                "eligible": r["eligible"],
                "status": r["status"],
                "reprocess_reason": r["reprocess_reason"],
                "stage": r["stage"],
                "knockout_reasons": [x["detail"] for x in (r["knockout"] or {}).get("reasons", [])],
                "explanation": r["explanation"],
                "explanation_source": r["explanation_source"],
            }
        )
    return items, total
