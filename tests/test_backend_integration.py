"""Opt-in PostgreSQL tests; optional TEST_BLOB_CONNECTION_STRING uses Azurite instead of memory storage."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from urllib.request import Request, urlopen

import httpx
import pytest
from sqlalchemy import delete, select, update

from backend.api.deps import Services
from backend.config import PlatformSettings
from backend.db import Database, TenantRepo
from backend.main import create_app
from backend.models import Candidate, Company, QueueJob, Resume, ScreeningResult
from backend.queue import PostgresQueue
from backend.runtime import build_services, close_services
from backend.services import screening
from backend.storage import InMemoryBlobStorage
from backend.worker import Worker, schedule_retention
from engine.extraction import extract_job_requirements
from tests.conftest import SAMPLE_DIR

pytestmark = pytest.mark.integration
JD = (SAMPLE_DIR / "jd.txt").read_text(encoding="utf-8")


@pytest.fixture
def settings() -> PlatformSettings:
    app_url, system_url = os.getenv("TEST_DATABASE_URL"), os.getenv("TEST_DATABASE_SYSTEM_URL")
    if not app_url or not system_url:
        pytest.skip("Set TEST_DATABASE_URL and TEST_DATABASE_SYSTEM_URL to a migrated local PostgreSQL database")
    return PlatformSettings(
        _env_file=None,
        APP_ENV="test",
        DATABASE_URL=app_url,
        DATABASE_SYSTEM_URL=system_url,
        DB_POOL_SIZE=0,
        JWT_SECRET="integration-jwt-secret-" * 3,
        PII_HASH_KEY="integration-pii-secret-" * 3,
        BLOB_CONNECTION_STRING=os.getenv("TEST_BLOB_CONNECTION_STRING") or None,
        BLOB_CONTAINER="phase2-tests",
        QUEUE_MAX_ATTEMPTS=2,
        QUEUE_BACKOFF_BASE_S=5,
    )


@asynccontextmanager
async def platform(settings: PlatformSettings) -> AsyncIterator[tuple[Services, httpx.AsyncClient, list[uuid.UUID]]]:
    svc = await build_services(settings)
    companies: list[uuid.UUID] = []
    app = create_app(svc=svc)
    try:
        async with svc.db.system_session() as session:
            if (await session.execute(select(Company.id).limit(1))).first():
                pytest.fail("Integration tests require an empty dedicated database with no other API/worker connected")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            yield svc, client, companies
    finally:
        # Only delete companies created by this test, with their associated blobs and cascading rows.
        async with svc.db.system_session() as session:
            names = (await session.execute(select(Resume.blob_name).where(Resume.company_id.in_(companies)))).scalars()
            for name in names:
                await svc.storage.delete(name)
            await session.execute(delete(Company).where(Company.id.in_(companies)))
        await close_services(svc)


async def register(client: httpx.AsyncClient, companies: list[uuid.UUID]) -> dict:
    response = await client.post(
        "/v1/auth/register-company",
        json={
            "company_name": "Phase 2 integration test",
            "admin_email": f"{uuid.uuid4().hex}@example.com",
            "admin_password": "Integration-password-123",
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()
    companies.append(uuid.UUID(token["company_id"]))
    client.headers["Authorization"] = f"Bearer {token['access_token']}"
    return token


async def prepare_run(svc: Services, client: httpx.AsyncClient) -> tuple[str, str, str, str]:
    response = await client.post("/v1/jobs", json={"title": "ML Engineer", "jd_text": JD})
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    # The runner must be gated on an explicitly confirmed requirements version.
    assert (await client.post(f"/v1/jobs/{job_id}/screening-runs")).status_code == 409
    requirements = extract_job_requirements(JD, None).requirements.model_dump(
        mode="json", exclude={"requirements_confirmed"}
    )
    response = await client.put(f"/v1/jobs/{job_id}/requirements", json=requirements)
    assert response.status_code == 201, response.text
    assert (await client.post(f"/v1/jobs/{job_id}/requirements/confirm", json={"version": 1})).status_code == 200
    assert (await client.post(f"/v1/jobs/{job_id}/requirements/confirm", json={"version": 1})).status_code == 409
    filename = "01_strong_match.pdf"
    data = (SAMPLE_DIR / "resumes" / filename).read_bytes()
    response = await client.post("/v1/uploads/sas", json={"filename": filename, "size": len(data)})
    assert response.status_code == 200, response.text
    upload = response.json()
    if isinstance(svc.storage, InMemoryBlobStorage):
        await svc.storage.put(upload["blob_name"], data)
    else:

        def put():
            with urlopen(
                Request(upload["upload_url"], data=data, headers=upload["headers"], method="PUT"), timeout=10
            ) as r:
                assert r.status == 201

        await asyncio.to_thread(put)
    response = await client.post(
        "/v1/resumes/register",
        json={
            "blob_name": upload["blob_name"],
            "filename": filename,
            "job_id": job_id,
            "consent": {"source": "integration form", "purpose": "recruitment screening"},
        },
    )
    assert response.status_code == 201, response.text
    resume_id = response.json()["resume"]["id"]
    assert response.json()["resume"]["status"] == "registered"
    assert await Worker(svc).run_once()
    response = await client.get(f"/v1/resumes/{resume_id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ready"
    candidate_id = response.json()["candidate_id"]
    response = await client.post(f"/v1/jobs/{job_id}/screening-runs")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued" and response.json()["done"] == 0
    return job_id, response.json()["id"], resume_id, candidate_id


def test_api_worker_and_results_survive_service_restart(
    settings: PlatformSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            token = await register(client, companies)
            assert (await client.get("/ready")).status_code == 200
            job_id, run_id, _, _ = await prepare_run(svc, client)
            # Recreate both database pools and worker, retaining the external storage dependency.
            replacement_db = Database.from_settings(settings)
            replacement = Services(settings, replacement_db, svc.storage, PostgresQueue(replacement_db, settings))
            try:
                started, finish = threading.Event(), threading.Event()
                original_assess = screening.assess

                def paused_assess(*args):
                    started.set()
                    if not finish.wait(timeout=5):
                        raise TimeoutError("Test did not release assessment")
                    return original_assess(*args)

                monkeypatch.setattr(screening, "assess", paused_assess)
                processing = asyncio.create_task(Worker(replacement).run_once())
                try:
                    assert await asyncio.to_thread(started.wait, 3)
                    in_progress = (await client.get(f"/v1/screening-runs/{run_id}")).json()
                    assert in_progress["status"] == "running" and in_progress["done"] == 0
                finally:
                    finish.set()
                    assert await processing
                app = create_app(svc=replacement)
                async with (
                    app.router.lifespan_context(app),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://restart",
                        headers={"Authorization": f"Bearer {token['access_token']}"},
                    ) as restarted,
                ):
                    run = (await restarted.get(f"/v1/screening-runs/{run_id}")).json()
                    assert run["status"] == "completed" and run["done"] == run["total"] == 1
                    assert run["finished_at"] is not None
                    page_response = await restarted.get(f"/v1/jobs/{job_id}/candidates")
                    assert page_response.status_code == 200, page_response.text
                    page = page_response.json()
                    assert page["total"] == 1 and page["items"][0]["status"] == "scored"
                    assert page["items"][0]["rank"] == 1
                    assert 0 <= page["items"][0]["score"] <= 100
                    assert page["items"][0]["explanation_source"] == "template"
            finally:
                await replacement_db.dispose()
            # Another tenant sees neither the first tenant's job nor its run.
            await register(client, companies)
            assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 404
            assert (await client.get(f"/v1/screening-runs/{run_id}")).status_code == 404
            assert (await client.get("/v1/jobs")).json() == []

    asyncio.run(scenario())


def test_queue_transactions_retries_and_stale_lease_fencing(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            token = await register(client, companies)
            cid = uuid.UUID(token["company_id"])
            with pytest.raises(RuntimeError, match="rollback"):
                async with svc.db.tenant_session(cid) as session:
                    await svc.queue.enqueue(
                        session, company_id=cid, kind="retention_sweep", payload={}, key=f"rollback:{cid}"
                    )
                    raise RuntimeError("rollback")
            assert await svc.queue.claim("rollback-check") is None
            async with svc.db.tenant_session(cid) as session:
                for _ in range(2):
                    await svc.queue.enqueue(
                        session, company_id=cid, kind="retention_sweep", payload={}, key=f"queue:{cid}"
                    )
            claims = await asyncio.gather(svc.queue.claim("worker-a"), svc.queue.claim("worker-b"))
            claimed = next(c for c in claims if c)
            assert sum(c is not None for c in claims) == 1
            assert await svc.queue.fail(claimed, "temporary")
            assert await svc.queue.claim("too-early") is None
            async with svc.db.system_session() as session:
                row = (await session.execute(select(QueueJob).where(QueueJob.id == claimed.id))).scalar_one()
                assert row.status == "queued" and row.attempts == 1
                assert (row.run_after - row.updated_at).total_seconds() == 5
                await session.execute(
                    update(QueueJob).where(QueueJob.id == claimed.id).values(run_after=datetime.now(UTC))
                )
            second = await svc.queue.claim("worker-c")
            assert second.attempts == 2
            async with svc.db.system_session() as session:
                await session.execute(
                    update(QueueJob)
                    .where(QueueJob.id == claimed.id)
                    .values(locked_at=datetime.now(UTC) - timedelta(seconds=settings.queue_lock_timeout_s + 10))
                )
            reclaimed = await svc.queue.claim("worker-d")
            assert reclaimed.attempts == 3  # Terminal crash recovery; the handler must not execute again.
            assert await svc.queue.complete(second) is False
            assert await svc.queue.fail(second, "stale failure") is False
            assert await svc.queue.release(second, "stale release") is False
            assert await svc.queue.renew(second) is False
            assert await svc.queue.renew(reclaimed) is True
            assert await svc.queue.fail(reclaimed, "exhausted") is True
            assert await svc.queue.claim("dead-check") is None
            async with svc.db.system_session() as session:
                row = (await session.execute(select(QueueJob).where(QueueJob.id == claimed.id))).scalar_one()
                assert row.status == "dead" and row.locked_at is None and row.locked_by is None

    asyncio.run(scenario())


def test_dead_screen_job_advances_run_and_dead_ingestion_is_visible(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            _, run_id, resume_id, _ = await prepare_run(svc, client)
            # Simulate a crash on the last attempt before terminal bookkeeping was written.
            claimed = await svc.queue.claim("crashed-worker")
            async with svc.db.system_session() as session:
                await session.execute(
                    update(QueueJob)
                    .where(QueueJob.id == claimed.id)
                    .values(
                        attempts=2, locked_at=datetime.now(UTC) - timedelta(seconds=settings.queue_lock_timeout_s + 10)
                    )
                )
            handler = AsyncMock()
            assert await Worker(svc, handlers={"screen_application": handler}).run_once()
            handler.assert_not_awaited()
            run = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert run["status"] == "completed" and run["done"] == 1
            async with svc.db.system_session() as session:
                result = (
                    await session.execute(select(ScreeningResult).where(ScreeningResult.run_id == uuid.UUID(run_id)))
                ).scalar_one()
                assert result.status == "needs_reprocess" and result.score is None
                resume = (await session.execute(select(Resume).where(Resume.id == uuid.UUID(resume_id)))).scalar_one()
                # A separate registered resume exercises the ingestion failure finalizer.
                failed = Resume(
                    id=uuid.uuid4(),
                    company_id=resume.company_id,
                    blob_name=resume.blob_name,
                    blob_uri=resume.blob_uri,
                    filename="missing.txt",
                    content_hash=uuid.uuid4().hex,
                    mime="text/plain",
                    size=1,
                    status="registered",
                )
                session.add(failed)
                await session.flush()
                await svc.queue.enqueue(
                    session,
                    company_id=resume.company_id,
                    kind="extract_resume",
                    payload={"resume_id": str(failed.id)},
                    key=f"failed:{failed.id}",
                )
            worker = Worker(svc, handlers={"extract_resume": AsyncMock(side_effect=OSError("storage offline"))})
            assert await worker.run_once()
            async with svc.db.system_session() as session:
                await session.execute(
                    update(QueueJob)
                    .where(QueueJob.idempotency_key == f"failed:{failed.id}")
                    .values(run_after=datetime.now(UTC))
                )
            assert await worker.run_once()
            failed_response = await client.get(f"/v1/resumes/{failed.id}")
            assert failed_response.json()["status"] == "failed"
            assert failed_response.json()["status_reason"] == "processing failed: OSError"

    asyncio.run(scenario())


def test_retention_schedule_is_idempotent_and_deletion_is_processed(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            token = await register(client, companies)
            cid = uuid.UUID(token["company_id"])
            candidate_id = uuid.uuid4()
            async with svc.db.tenant_session(cid) as session:
                TenantRepo(session, cid).add(
                    Candidate(id=candidate_id, last_activity_at=datetime.now(UTC) - timedelta(days=200))
                )
            now = datetime(2026, 10, 5, settings.retention_sweep_hour_utc, tzinfo=UTC)
            await schedule_retention(svc, now=now)
            await schedule_retention(svc, now=now)
            async with svc.db.system_session() as session:
                kinds = (await session.execute(select(QueueJob.kind).where(QueueJob.company_id == cid))).scalars().all()
                assert kinds == ["retention_sweep"]
            assert await Worker(svc).run_once()
            assert await Worker(svc).run_once()
            async with svc.db.system_session() as session:
                assert (await session.execute(select(Candidate.id).where(Candidate.id == candidate_id))).first() is None
                statuses = (
                    (await session.execute(select(QueueJob.status).where(QueueJob.company_id == cid))).scalars().all()
                )
                assert statuses == ["done", "done"]

    asyncio.run(scenario())


def test_refresh_rotation_reuse_commits_revocation_and_role_permissions(settings: PlatformSettings) -> None:
    async def scenario():
        async with platform(settings) as (_svc, client, companies):
            token = await register(client, companies)
            response = await client.post(
                "/v1/users",
                json={
                    "email": f"{uuid.uuid4().hex}@example.com",
                    "password": "Integration-password-123",
                    "role": "hiring_manager",
                },
            )
            assert response.status_code == 201, response.text
            manager_email = response.json()["email"]
            rotated = await client.post("/v1/auth/refresh", json={"refresh_token": token["refresh_token"]})
            assert rotated.status_code == 200
            reused = await client.post("/v1/auth/refresh", json={"refresh_token": token["refresh_token"]})
            assert reused.status_code == 401
            assert (
                await client.post("/v1/auth/refresh", json={"refresh_token": rotated.json()["refresh_token"]})
            ).status_code == 401
            login = await client.post(
                "/v1/auth/login", json={"email": manager_email, "password": "Integration-password-123"}
            )
            assert login.status_code == 200
            client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
            assert (await client.get("/v1/jobs")).status_code == 200
            assert (await client.get("/v1/users")).status_code == 403
            assert (await client.post("/v1/jobs", json={"title": "ML Engineer", "jd_text": JD})).status_code == 403

    asyncio.run(scenario())
