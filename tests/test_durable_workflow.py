from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from sqlalchemy import select, update

from backend.api.deps import Services
from backend.config import PlatformSettings
from backend.db import Database
from backend.models import (
    Application,
    CandidateContact,
    Consent,
    ExtractionCacheEntry,
    QueueJob,
    ScreeningInput,
    ScreeningResult,
    ScreeningRun,
    UsageEvent,
)
from backend.queue import ClaimedJob, PostgresQueue
from backend.services import resumes, screening
from backend.snapshots import SnapshotMismatch, restore_settings
from backend.storage import InMemoryBlobStorage
from backend.worker import Worker
from engine.config import Settings, get_settings
from engine.extraction import extract_job_requirements
from engine.llm import LLMError
from engine.local_ai import OllamaProvider
from engine.parsing import parse_bytes
from engine.schemas import JobRequirements, LLMUsage
from tests.conftest import SAMPLE_DIR
from tests.test_backend_integration import JD, platform, prepare_run, register
from tests.test_backend_integration import settings as settings

pytestmark = pytest.mark.integration


async def queued_kinds(svc, company_id: uuid.UUID) -> set[str]:
    async with svc.db.system_session() as s:
        rows = await s.execute(
            select(QueueJob.kind).where(QueueJob.company_id == company_id, QueueJob.status == "queued")
        )
        return set(rows.scalars())


async def make_job(client) -> str:
    response = await client.post("/v1/jobs", json={"title": "ML Engineer", "jd_text": JD})
    assert response.status_code == 201
    job_id = response.json()["id"]
    requirements = extract_job_requirements(JD, None).requirements.model_dump(
        mode="json", exclude={"requirements_confirmed"}
    )
    assert (await client.put(f"/v1/jobs/{job_id}/requirements", json=requirements)).status_code == 201
    assert (await client.post(f"/v1/jobs/{job_id}/requirements/confirm", json={"version": 1})).status_code == 200
    return job_id


async def upload(svc, client, filename: str, data: bytes | None = None) -> dict:
    data = data if data is not None else (SAMPLE_DIR / "resumes" / filename).read_bytes()
    response = await client.post("/v1/uploads/sas", json={"filename": filename, "size": len(data)})
    assert response.status_code == 200
    target = response.json()
    if isinstance(svc.storage, InMemoryBlobStorage):
        await svc.storage.put(target["blob_name"], data)
    else:

        def put():
            with urlopen(
                Request(target["upload_url"], data=data, headers=target["headers"], method="PUT"), timeout=10
            ) as r:
                assert r.status == 201

        await asyncio.to_thread(put)
    return {**target, "filename": filename}


def item(target: dict, job_id: str, *, consent: bool = True) -> dict:
    body = {"blob_name": target["blob_name"], "filename": target["filename"], "job_id": job_id}
    if consent:
        body["consent"] = {"source": "application form", "purpose": "recruitment"}
    return body


async def accelerate(svc, key: str | None = None) -> None:
    async with svc.db.system_session() as session:
        query = update(QueueJob).where(QueueJob.status == "queued")
        if key:
            query = query.where(QueueJob.idempotency_key == key)
        await session.execute(query.values(run_after=datetime.now(UTC)))


def test_bulk_duplicates_link_jobs_and_recruiter_screens_multiple_candidates(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            token = await register(client, companies)
            job_a, job_b = await make_job(client), await make_job(client)
            strong = await upload(svc, client, "01_strong_match.pdf")
            duplicate = await upload(svc, client, "01_strong_match.pdf")
            intern = await upload(svc, client, "05_intern.txt")
            invalid = await upload(svc, client, "invalid.pdf", b"This is not a PDF")
            missing = {"blob_name": f"{token['company_id']}/{uuid.uuid4().hex}/missing.txt", "filename": "missing.txt"}
            response = await client.post(
                "/v1/resumes/register-bulk",
                json={
                    "items": [
                        item(strong, job_a),
                        item(duplicate, job_b),
                        item(intern, job_a, consent=False),
                        item(invalid, job_a),
                        item(missing, job_a),
                    ]
                },
            )
            assert response.status_code == 200
            batch = response.json()
            assert [entry["status_code"] for entry in batch] == [201, 200, 201, 415, 404]
            assert batch[0]["resume"]["id"] == batch[1]["resume"]["id"]
            assert not await svc.storage.exists(duplicate["blob_name"])
            # Replaying a successful duplicate registration works after its redundant blob was removed.
            replay = await client.post("/v1/resumes/register", json=item(duplicate, job_b))
            assert replay.status_code == 200 and replay.json()["created"] is False
            assert await Worker(svc).run_once()
            assert await Worker(svc).run_once()
            strong_resume = (await client.get(f"/v1/resumes/{batch[0]['resume']['id']}")).json()
            intern_resume = (await client.get(f"/v1/resumes/{batch[2]['resume']['id']}")).json()
            assert intern_resume["status"] == "ready"
            async with svc.db.system_session() as session:
                apps = (
                    (
                        await session.execute(
                            select(Application).where(
                                Application.candidate_id == uuid.UUID(strong_resume["candidate_id"])
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert {str(app.job_id) for app in apps} == {job_a, job_b}
                grants = (
                    (
                        await session.execute(
                            select(Consent).where(Consent.candidate_id == uuid.UUID(strong_resume["candidate_id"]))
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(grants) == 2
            blocked_run = (await client.post(f"/v1/jobs/{job_a}/screening-runs")).json()
            assert blocked_run["total"] == 1 and len(blocked_run["blocked"]) == 1
            assert await Worker(svc).run_once()
            assert (await client.post("/v1/resumes/register", json=item(intern, job_a))).status_code == 200
            accepted = (await client.post(f"/v1/jobs/{job_a}/screening-runs")).json()
            assert accepted["total"] == 2 and accepted["blocked"] == []
            replacement_db = Database.from_settings(settings)
            replacement = Services(settings, replacement_db, svc.storage, PostgresQueue(replacement_db, settings))
            try:
                assert all(await asyncio.gather(Worker(replacement).run_once(0), Worker(replacement).run_once(1)))
            finally:
                await replacement_db.dispose()
            run = (await client.get(f"/v1/screening-runs/{accepted['id']}")).json()
            assert run["status"] == "completed" and run["done"] == 2
            page = (await client.get(f"/v1/jobs/{job_a}/candidates")).json()
            assert page["total"] == 2 and page["items"][0]["candidate_id"] == strong_resume["candidate_id"]
            assert page["items"][0]["rank"] == 1 and page["items"][1]["rank"] is None
            assert (
                await client.post(
                    f"/v1/applications/{page['items'][0]['application_id']}/decision",
                    json={"decision": "shortlist", "reason": "Verified skills match"},
                )
            ).status_code == 201
            assert (await client.get(f"/v1/jobs/{job_a}/candidates", params={"stage": "shortlisted"})).json()[
                "total"
            ] == 1

    asyncio.run(scenario())


def test_500_resume_batch_survives_worker_restart_and_screens_every_candidate(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id = await make_job(client)
            targets = []
            for index in range(500):
                data = (
                    f"Synthetic Applicant\nresume-{index}@example.com\n"
                    "Skills: Python, PyTorch, SQL, Docker\n"
                    "ML Engineer at Example Studio | Jan 2021 - Present\n"
                    "Built machine learning services using Python and PyTorch.\n"
                    "Location: Bangalore\nNotice period: 30 days\n"
                ).encode()
                targets.append(item(await upload(svc, client, f"batch-{index}.txt", data), job_id))
            assert (await client.post("/v1/resumes/register-bulk", json={"items": []})).status_code == 422
            assert (
                await client.post("/v1/resumes/register-bulk", json={"items": [*targets, targets[0]]})
            ).status_code == 413
            response = await client.post("/v1/resumes/register-bulk", json={"items": targets})
            assert response.status_code == 200
            batch = response.json()
            assert len(batch) == 500 and all(entry["status_code"] == 201 for entry in batch)
            assert len({entry["resume"]["id"] for entry in batch}) == 500
            # No live/paid model calls: the test uses the suite's offline rules profile.
            # Replace the worker/database connection before claiming the durable jobs.
            replacement_db = Database.from_settings(settings)
            replacement = Services(settings, replacement_db, svc.storage, PostgresQueue(replacement_db, settings))
            try:
                worker = Worker(replacement)
                for _ in range(500):
                    assert await worker.run_once()
                accepted = await client.post(f"/v1/jobs/{job_id}/screening-runs")
                assert accepted.status_code == 202
                run = accepted.json()
                assert run["total"] == 500 and run["blocked"] == []
                # Screening advances the same durable queue; no in-memory batch survives by accident.
                for _ in range(500):
                    assert await worker.run_once()
                completed = (await client.get(f"/v1/screening-runs/{run['id']}")).json()
                assert completed["status"] == "completed"
                assert completed["done"] == completed["total"] == 500
                results = (
                    await client.get(f"/v1/jobs/{job_id}/candidates", params={"run_id": run["id"], "limit": 200})
                ).json()
                assert results["total"] == 500 and len(results["items"]) == 200
            finally:
                await replacement_db.dispose()

    asyncio.run(scenario())


def test_local_run_pins_models_and_worker_uses_snapshot_after_config_changes(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_request = OllamaProvider._request

    def mocked_request(self, path, body=None):
        if path == "/api/tags":
            return {
                "models": [
                    {"name": "shortlist-local:v1", "digest": "chat-v1"},
                    {"name": "nomic-embed-text:v1.5", "digest": "embed-v1"},
                ]
            }
        if path == "/api/embed":
            return {"embeddings": [[1, 0]] * len(body["input"]), "prompt_eval_count": 50}
        if path == "/api/chat":
            assert body["model"] == "shortlist-local:v1"
            # An unavailable explanation safely uses the existing deterministic explanation template.
            if "skills" not in body["format"]["properties"]:
                raise LLMError("explanation unavailable")
            from engine.extraction import rule_based_candidate
            from engine.normalize import load_ontology

            source = body["messages"][1]["content"]
            extraction = rule_based_candidate(source, load_ontology())
            return {
                "message": {"content": extraction.model_dump_json()},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 100,
                "eval_count": 50,
            }
        return original_request(self, path, body)

    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id, _, _, _ = await prepare_run(svc, client)
            assert await Worker(svc).run_once()
            config = Settings(
                _env_file=None,
                LLM_PROVIDER="ollama",
                EXTRACTION_MODE="llm",
                SEMANTIC_BACKEND="embeddings",
                OCR_PROVIDER="tesseract",
                LLM_MAX_RETRIES=0,
            )
            monkeypatch.setattr(OllamaProvider, "_request", mocked_request)
            monkeypatch.setattr(screening, "get_engine_settings", lambda: config)
            response = await client.post(f"/v1/jobs/{job_id}/screening-runs")
            assert response.status_code == 202, response.text
            run_id = response.json()["id"]
            async with svc.db.system_session() as session:
                run = await session.get(ScreeningRun, uuid.UUID(run_id))
                assert run.versions["provider"] == "ollama" and run.versions["model_digest"] == "chat-v1"
                assert run.snapshot["settings"]["ollama_embedding_digest"] == "embed-v1"
            # A new worker/environment must execute the frozen local config, not the current rules config.
            monkeypatch.setattr(screening, "get_engine_settings", get_settings)
            assert await Worker(svc).run_once()
            final = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert final["status"] == "completed" and final["done"] == 1 and final["cost_usd"] == 0
            async with svc.db.system_session() as session:
                result = (
                    await session.execute(select(ScreeningResult).where(ScreeningResult.run_id == uuid.UUID(run_id)))
                ).scalar_one()
                assert result.status == "scored" and result.extraction_meta["model"] == "shortlist-local:v1"
                usages = (
                    (await session.execute(select(UsageEvent).where(UsageEvent.run_id == uuid.UUID(run_id))))
                    .scalars()
                    .all()
                )
                assert usages and all(usage.cost_usd == 0 for usage in usages)

    asyncio.run(scenario())


def test_missing_local_server_returns_503_without_creating_partial_run(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id, _, _, _ = await prepare_run(svc, client)
            assert await Worker(svc).run_once()
            config = Settings(
                _env_file=None,
                LLM_PROVIDER="ollama",
                EXTRACTION_MODE="llm",
                SEMANTIC_BACKEND="embeddings",
                OCR_PROVIDER="tesseract",
            )
            monkeypatch.setattr(screening, "get_engine_settings", lambda: config)

            def fail(*args):
                raise LLMError("Local Ollama unavailable")

            monkeypatch.setattr(OllamaProvider, "model_digest", fail)
            response = await client.post(f"/v1/jobs/{job_id}/screening-runs")
            assert response.status_code == 503 and "unavailable" in response.text
            async with svc.db.system_session() as session:
                runs = (await session.execute(select(ScreeningRun))).scalars().all()
                assert len(runs) == 1

    asyncio.run(scenario())


def test_frozen_inputs_ignore_new_resume_requirements_and_worker_settings(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id, run_id, resume_id, candidate_id = await prepare_run(svc, client)
            original_run = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            held = await svc.queue.claim("paused-screen-worker")
            async with svc.db.system_session() as session:
                frozen = (
                    await session.execute(select(ScreeningInput).where(ScreeningInput.run_id == uuid.UUID(run_id)))
                ).scalar_one()
                contact = (
                    await session.execute(
                        select(CandidateContact).where(CandidateContact.candidate_id == uuid.UUID(candidate_id))
                    )
                ).scalar_one()
                assert contact.email
            newer = await upload(
                svc, client, "new-version.txt", f"Updated Resume\nEmail: {contact.email}\nSkills: Excel\n".encode()
            )
            registered = await client.post("/v1/resumes/register", json=item(newer, job_id))
            assert registered.status_code == 201
            assert await Worker(svc).run_once()  # Old screen job is held; ingest the newer resume first.
            new_resume = (await client.get(f"/v1/resumes/{registered.json()['resume']['id']}")).json()
            assert new_resume["candidate_id"] == candidate_id and new_resume["version"] == 2
            requirements = {**original_run["snapshot"]["requirements"], "experience_min_years": 100}
            requirements.pop("requirements_confirmed")
            assert (await client.put(f"/v1/jobs/{job_id}/requirements", json=requirements)).status_code == 201
            assert (
                await client.post(f"/v1/jobs/{job_id}/requirements/confirm", json={"version": 2})
            ).status_code == 200
            saved_settings = restore_settings(original_run["snapshot"]["settings"], get_settings())
            expected = screening.assess(
                JobRequirements.model_validate(original_run["snapshot"]["requirements"]),
                frozen.redacted_text,
                None,
                None,
                saved_settings,
                datetime.fromisoformat(original_run["reference_date"]).date(),
            )
            changed = get_settings().model_copy(
                update={
                    "skill_recency_full_months": 60,
                    "max_resume_chars": 5,
                    "score_weights": {"required_skill_coverage": 1.0},
                    "extraction_mode": "llm",
                    "semantic_backend": "embeddings",
                    "azure_openai_endpoint": "https://changed.invalid/",
                    "azure_openai_api_key": "different-secret",
                }
            )
            monkeypatch.setattr(screening, "get_engine_settings", lambda: changed)
            await screening.process_screen_application(svc.db, held)
            assert await svc.queue.complete(held)
            async with svc.db.system_session() as session:
                actual = (
                    await session.execute(select(ScreeningResult).where(ScreeningResult.run_id == uuid.UUID(run_id)))
                ).scalar_one()
                assert actual.score == expected.result["score"] and actual.features == expected.result["features"]
                assert actual.score_breakdown == expected.result["score_breakdown"]
            inputs = (await client.get(f"/v1/screening-runs/{run_id}/inputs")).json()
            assert inputs[0]["resume_id"] == resume_id and inputs[0]["resume_version"] == 1
            # An old resume registered again does not replace the application's newer version.
            original_upload = await upload(svc, client, "01_strong_match.pdf")
            assert (await client.post("/v1/resumes/register", json=item(original_upload, job_id))).status_code == 200
            async with svc.db.system_session() as session:
                app = (
                    await session.execute(select(Application).where(Application.id == frozen.application_id))
                ).scalar_one()
                assert str(app.resume_id) == new_resume["id"]
                assert app.stage == "applied"  # The older run cannot mark the replacement resume screened.

    asyncio.run(scenario())


def test_consent_withdrawal_before_and_during_screening_and_historical_retry(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            _, run_id, _, candidate_id = await prepare_run(svc, client)
            original_assess = screening.assess
            blocked_assess = Mock(side_effect=AssertionError("No screening without consent"))
            monkeypatch.setattr(screening, "assess", blocked_assess)
            assert (await client.post(f"/v1/candidates/{candidate_id}/consents/revoke")).status_code == 200
            assert await Worker(svc).run_once()
            blocked_assess.assert_not_called()
            assert (await client.post(f"/v1/screening-runs/{run_id}/retry")).status_code == 409
            assert (
                await client.post(
                    f"/v1/candidates/{candidate_id}/consents", json={"source": "new form", "purpose": "recruitment"}
                )
            ).status_code == 201
            retry = await client.post(f"/v1/screening-runs/{run_id}/retry")
            assert retry.status_code == 202
            retry_id = retry.json()["id"]
            assert retry.json()["retry_of_run_id"] == run_id
            started, finish = threading.Event(), threading.Event()

            def pause(*args):
                started.set()
                assert finish.wait(timeout=5)
                return original_assess(*args)

            monkeypatch.setattr(screening, "assess", pause)
            processing = asyncio.create_task(Worker(svc).run_once())
            try:
                assert await asyncio.to_thread(started.wait, 3)
                assert (await client.post(f"/v1/candidates/{candidate_id}/consents/revoke")).status_code == 200
            finally:
                finish.set()
                assert await processing
            async with svc.db.system_session() as session:
                result = (
                    await session.execute(select(ScreeningResult).where(ScreeningResult.run_id == uuid.UUID(retry_id)))
                ).scalar_one()
                assert result.status == "needs_reprocess" and result.score is None
                assert result.reprocess_reason == "no consent record"
                assert (await session.execute(select(ExtractionCacheEntry))).first() is None
            assert (
                await client.post(
                    f"/v1/candidates/{candidate_id}/consents", json={"source": "latest form", "purpose": "recruitment"}
                )
            ).status_code == 201
            latest = (await client.post(f"/v1/screening-runs/{retry_id}/retry")).json()
            monkeypatch.setattr(screening, "assess", original_assess)
            assert await Worker(svc).run_once()
            latest_run = (await client.get(f"/v1/screening-runs/{latest['id']}")).json()
            original_run = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert latest_run["status"] == "completed" and original_run["done"] == 1
            assert latest_run["snapshot"] == original_run["snapshot"]
            assert latest_run["reference_date"] == original_run["reference_date"]

    asyncio.run(scenario())


def test_failed_ingestion_reprocessing_has_fresh_generation_and_old_jobs_cannot_overwrite(
    settings: PlatformSettings,
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id = await make_job(client)
            target = await upload(svc, client, "01_strong_match.pdf")
            accepted = (await client.post("/v1/resumes/register", json=item(target, job_id))).json()["resume"]
            failing = Worker(svc, handlers={"extract_resume": AsyncMock(side_effect=OSError())})
            assert await failing.run_once()
            await accelerate(svc)
            assert await failing.run_once()
            assert (await client.get(f"/v1/resumes/{accepted['id']}")).json()["status"] == "failed"
            responses = await asyncio.gather(
                client.post(f"/v1/resumes/{accepted['id']}/reprocess"),
                client.post(f"/v1/resumes/{accepted['id']}/reprocess"),
            )
            assert sorted(r.status_code for r in responses) == [202, 409]
            assert await Worker(svc).run_once()
            ready = (await client.get(f"/v1/resumes/{accepted['id']}")).json()
            assert ready["status"] == "ready" and ready["processing_generation"] == ready["processed_generation"] == 2
            old = ClaimedJob(
                uuid.uuid4(),
                uuid.UUID(companies[0].hex),
                "extract_resume",
                {"resume_id": accepted["id"], "generation": 1},
                2,
                2,
            )
            await resumes.record_dead_extract_job(svc.db, old, "stale failure")
            await resumes.process_extract_resume(svc.db, svc.storage, settings, old)
            assert (await client.get(f"/v1/resumes/{accepted['id']}")).json() == ready
            assert (await client.post(f"/v1/jobs/{job_id}/screening-runs")).status_code == 202
            assert await Worker(svc).run_once()

    asyncio.run(scenario())


def test_scanned_resume_can_be_reprocessed_with_ocr_without_duplicate_candidate_or_consent(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id = await make_job(client)
            target = await upload(svc, client, "08_scanned_needs_ocr.pdf")
            accepted = (await client.post("/v1/resumes/register", json=item(target, job_id))).json()["resume"]
            assert await Worker(svc).run_once()
            first = (await client.get(f"/v1/resumes/{accepted['id']}")).json()
            assert first["status"] == "needs_ocr"
            assert (await client.post(f"/v1/jobs/{job_id}/screening-runs")).status_code == 409
            assert (await client.post(f"/v1/resumes/{accepted['id']}/reprocess")).status_code == 202
            doc = parse_bytes(b"Skills: Python, Machine Learning, SQL\n", "ocr.txt").model_copy(
                update={"ocr_applied": True, "ocr_pages": 1}
            )
            monkeypatch.setattr(resumes, "parse_bytes", lambda *_args: doc)
            assert await Worker(svc).run_once()
            ready = (await client.get(f"/v1/resumes/{accepted['id']}")).json()
            assert (
                ready["status"] == "ready"
                and ready["candidate_id"] == first["candidate_id"]
                and ready["version"] == first["version"]
            )
            async with svc.db.system_session() as session:
                grants = (
                    (
                        await session.execute(
                            select(Consent).where(Consent.candidate_id == uuid.UUID(ready["candidate_id"]))
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(grants) == 1
            assert (await client.post(f"/v1/jobs/{job_id}/screening-runs")).status_code == 202
            assert await Worker(svc).run_once()

    asyncio.run(scenario())


def test_parallel_registration_deduplicates_and_sas_cannot_overwrite(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id = await make_job(client)
            targets = [await upload(svc, client, "01_strong_match.pdf") for _ in range(2)]
            responses = await asyncio.gather(
                *(client.post("/v1/resumes/register", json=item(t, job_id)) for t in targets)
            )
            assert sorted(r.status_code for r in responses) == [200, 201]
            assert len({r.json()["resume"]["id"] for r in responses}) == 1
            kept = targets[next(i for i, r in enumerate(responses) if r.status_code == 201)]
            if not isinstance(svc.storage, InMemoryBlobStorage):

                def overwrite():
                    with pytest.raises(HTTPError) as error:
                        urlopen(
                            Request(kept["upload_url"], data=b"changed", headers=kept["headers"], method="PUT"),
                            timeout=10,
                        )
                    assert error.value.code == 403

                await asyncio.to_thread(overwrite)
            assert await Worker(svc).run_once()
            # One extraction for both registrations; only unrelated background work (pool matching) may remain.
            assert "extract_resume" not in await queued_kinds(svc, companies[-1])

    asyncio.run(scenario())


def test_erasure_removes_frozen_text_and_finishes_pending_run(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            _, run_id, _, candidate_id = await prepare_run(svc, client)
            assert (await client.delete(f"/v1/candidates/{candidate_id}")).status_code == 200
            run = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert run["status"] == "completed" and run["total"] == run["done"] == 0
            assert (await client.get(f"/v1/screening-runs/{run_id}/inputs")).json() == []
            assert not {"screen_application", "extract_resume"} & await queued_kinds(svc, companies[-1])

    asyncio.run(scenario())


@pytest.mark.parametrize("shared_cache", [True, False])
def test_extraction_checkpoint_survives_scoring_failure_and_new_run_retry(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch, shared_cache: bool
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            config = get_settings().model_copy(update={"extraction_cache_enabled": shared_cache})
            monkeypatch.setattr(screening, "get_engine_settings", lambda: config)
            _, run_id, _, _ = await prepare_run(svc, client)
            original_extract, original_assess = screening.extract_candidate, screening.assess

            def extract(*args, **kwargs):
                result = original_extract(*args, **kwargs)
                return result.model_copy(
                    update={
                        "usage": LLMUsage(
                            provider="fake", model="fake-model", prompt_tokens=100, completion_tokens=50, cost_usd=0.01
                        )
                    }
                )

            extractor = Mock(side_effect=extract)
            monkeypatch.setattr(screening, "extract_candidate", extractor)
            monkeypatch.setattr(screening, "assess", Mock(side_effect=RuntimeError("scorer unavailable")))
            assert await Worker(svc).run_once()
            async with svc.db.system_session() as session:
                checkpoint = (
                    await session.execute(select(ScreeningInput).where(ScreeningInput.run_id == uuid.UUID(run_id)))
                ).scalar_one()
                assert checkpoint.extraction["status"] == "ok"
                assert bool((await session.execute(select(ExtractionCacheEntry))).first()) is shared_cache
            await accelerate(svc)
            assert await Worker(svc).run_once()
            assert extractor.call_count == 1
            exhausted = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert exhausted["status"] == "completed" and exhausted["prompt_tokens"] == 100
            assert exhausted["completion_tokens"] == 50 and exhausted["cost_usd"] == 0.01
            retried = await client.post(f"/v1/screening-runs/{run_id}/retry")
            assert retried.status_code == 202
            monkeypatch.setattr(screening, "assess", original_assess)
            assert await Worker(svc).run_once()
            assert extractor.call_count == 1
            final_run = (await client.get(f"/v1/screening-runs/{retried.json()['id']}")).json()
            assert final_run["done"] == 1 and final_run["cost_usd"] == 0
            async with svc.db.system_session() as session:
                rows = (await session.execute(select(UsageEvent).where(UsageEvent.prompt_tokens > 0))).scalars().all()
                assert len(rows) == 1

    asyncio.run(scenario())


def test_incompatible_worker_fails_explicitly_and_frozen_inputs_cannot_be_edited(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            _, run_id, _, candidate_id = await prepare_run(svc, client)
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError):
                async with svc.db.tenant_session(companies[0]) as session:
                    await session.execute(
                        update(ScreeningInput)
                        .where(ScreeningInput.run_id == uuid.UUID(run_id))
                        .values(redacted_text="changed")
                    )
            original_validate = screening.validate_snapshot
            monkeypatch.setattr(screening, "validate_snapshot", Mock(side_effect=SnapshotMismatch()))
            extractor = Mock(side_effect=AssertionError("Do not use incompatible engine"))
            monkeypatch.setattr(screening, "extract_candidate", extractor)
            assert await Worker(svc).run_once()
            await accelerate(svc)
            assert await Worker(svc).run_once()
            extractor.assert_not_called()
            assert (await client.get(f"/v1/screening-runs/{run_id}")).json()["status"] == "completed"
            assert (await client.post(f"/v1/screening-runs/{run_id}/retry")).status_code == 409
            monkeypatch.setattr(screening, "validate_snapshot", original_validate)
            assert (await client.get(f"/v1/candidates/{candidate_id}")).status_code == 200

    asyncio.run(scenario())
