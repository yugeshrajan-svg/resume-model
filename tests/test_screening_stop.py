"""Stop is tenant-scoped, durable, and wins against an in-flight assessment."""

import asyncio
import threading
import uuid

import pytest
from sqlalchemy import select

from backend.models import QueueJob, ScreeningResult
from backend.services import screening
from backend.worker import Worker
from tests.test_backend_integration import platform, prepare_run, register
from tests.test_backend_integration import settings as settings
from tests.test_durable_workflow import item, upload
from tests.test_hr_management import add_user, auth

pytestmark = pytest.mark.integration


def test_stop_keeps_completed_results_and_progress(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            job_id, first_run, _, _ = await prepare_run(svc, client)
            assert await Worker(svc).run_once()
            assert (await client.get(f"/v1/screening-runs/{first_run}")).json()["status"] == "completed"
            target = await upload(svc, client, "05_intern.txt")
            assert (await client.post("/v1/resumes/register", json=item(target, job_id))).status_code == 201
            assert await Worker(svc).run_once()
            run = (await client.post(f"/v1/jobs/{job_id}/screening-runs")).json()
            assert run["total"] == 2
            assert await Worker(svc).run_once()
            stopped = (await client.post(f"/v1/screening-runs/{run['id']}/stop")).json()
            assert stopped["status"] == "stopped" and stopped["done"] == 1
            page = (await client.get(f"/v1/jobs/{job_id}/candidates", params={"run_id": run["id"]})).json()
            assert page["total"] == 1
            while await Worker(svc).run_once():
                pass
            latest = (await client.get(f"/v1/screening-runs/{run['id']}")).json()
            assert latest["status"] == "stopped" and latest["done"] == 1

    asyncio.run(scenario())


def test_stop_cancels_pending_work_and_is_authorized_and_idempotent(settings):
    async def scenario():
        async with platform(settings) as (svc, client, companies):
            admin = await register(client, companies)
            job_id, run_id, _, _ = await prepare_run(svc, client)
            manager = await add_user(client, admin, "hiring_manager")
            assert (await client.post(f"/v1/screening-runs/{run_id}/stop", headers=manager)).status_code == 403
            await register(client, companies)
            assert (await client.post(f"/v1/screening-runs/{run_id}/stop")).status_code == 404
            client.headers.update(auth(admin))
            response = await client.post(f"/v1/screening-runs/{run_id}/stop")
            assert response.status_code == 200, response.text
            stopped = response.json()
            assert stopped["status"] == "stopped" and stopped["done"] == 0 and stopped["total"] == 1
            again = (await client.post(f"/v1/screening-runs/{run_id}/stop")).json()
            assert again["finished_at"] == stopped["finished_at"]
            async with svc.db.system_session() as s:
                status = await s.scalar(
                    select(QueueJob.status).where(
                        QueueJob.kind == "screen_application",
                        QueueJob.payload["run_id"].as_string() == run_id,
                    )
                )
                assert status == "cancelled"
            replacement = (await client.post(f"/v1/jobs/{job_id}/screening-runs")).json()
            assert replacement["id"] != run_id and replacement["status"] == "queued"

    asyncio.run(scenario())


def test_stop_discards_inflight_results_and_dead_job_bookkeeping(settings, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = screening.assess

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(screening, "assess", delayed)

    async def scenario():
        async with platform(settings) as (svc, client, companies):
            await register(client, companies)
            _, run_id, _, _ = await prepare_run(svc, client)
            claimed = await svc.queue.claim("stop-race-test")
            assert claimed.kind == "screen_application"
            worker = Worker(svc)
            task = asyncio.create_task(worker._execute(claimed))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                response = await client.post(f"/v1/screening-runs/{run_id}/stop")
                assert response.status_code == 200 and response.json()["status"] == "stopped"
            finally:
                release.set()
                await task
            await worker._record_dead(claimed, "test failure")
            run = (await client.get(f"/v1/screening-runs/{run_id}")).json()
            assert run["status"] == "stopped" and run["done"] == 0
            async with svc.db.system_session() as s:
                assert (
                    not (
                        await s.execute(
                            select(ScreeningResult).where(
                                ScreeningResult.run_id == uuid.UUID(run_id),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

    asyncio.run(scenario())
